"""Read-only SQLite rowid table reader for QQ's nonstandard lock-byte page.

No page repair, row skipping or source writes. EncryptedDatabase authenticates
every accessed page. Ordinary databases continue to use SQLite's query engine.
"""
from __future__ import annotations
import struct

class RecordFormatError(ValueError):pass

def malformed(reason):raise RecordFormatError('QQ 数据记录结构异常，已停止读取：'+reason)

def varint(data,pos=0):
    value=0
    for i in range(9):
        if pos+i>=len(data):malformed('变长整数截断')
        byte=data[pos+i]
        if i==8:return (value<<8)|byte,pos+9
        value=(value<<7)|(byte&127)
        if byte<128:return value,pos+i+1
    raise AssertionError

def signed64(value):return value-(1<<64) if value>=1<<63 else value

def serial_size(code):
    if code in (10,11):malformed('保留的字段类型')
    return (0,1,2,3,4,6,8,8,0,0)[code] if code<10 else (code-12)//2

class Payload:
    def __init__(self,reader,page,offset,size):
        self.reader=reader;self.size=size
        if size<0 or size>1_000_000_000:malformed('记录长度越界')
        usable=reader.usable;minimum=((usable-12)*32//255)-23;maximum=usable-35
        local=size if size<=maximum else minimum+(size-minimum)%(usable-4)
        if local>maximum:local=minimum
        if offset+local+(4 if size>local else 0)>usable:malformed('记录跨越页面边界')
        self.local=page[offset:offset+local];self.pages=[];self.seen=set()
        self.next=int.from_bytes(page[offset+local:offset+local+4],'big') if size>local else 0
        if size>local and not self.next:malformed('缺少溢出页')
    def read(self,start,size):
        if start<0 or start+size>self.size:malformed('字段超过记录长度')
        if size>64*1024*1024:malformed('单个字段超过 64 MiB 读取上限')
        end=start+size;result=bytearray()
        if start<len(self.local):result+=self.local[start:min(end,len(self.local))];start+=len(result)
        width=self.reader.usable-4
        while start<end:
            index=(start-len(self.local))//width
            while len(self.pages)<=index:
                n=self.next
                if not n or n in self.seen:malformed('溢出链截断或循环')
                self.seen.add(n);b=self.reader.page(n);self.pages.append(n);self.next=int.from_bytes(b[:4],'big')
            b=self.reader.page(self.pages[index]);within=(start-len(self.local))%width;take=min(width-within,end-start)
            result+=b[4+within:4+within+take];start+=take
        return bytes(result)
    def fields(self,columns,wanted,rowid,alias):
        prefix=self.read(0,min(9,self.size));header,pos=varint(prefix)
        if header>self.size or header>1024*1024:malformed('记录头长度异常')
        raw=self.read(0,header);serials=[]
        while pos<header:
            code,pos=varint(raw,pos);serials.append(code)
        if pos!=header or len(serials)>len(columns):malformed('记录字段数量异常')
        lengths=[serial_size(x) for x in serials]
        if header+sum(lengths)!=self.size:malformed('字段长度与记录不一致')
        out={name:None for name in wanted};offset=header
        for index,(code,length) in enumerate(zip(serials,lengths)):
            name=columns[index]
            if name in wanted:
                if code==0:value=rowid if name==alias else None
                elif code in (8,9):value=code-8
                else:
                    data=self.read(offset,length)
                    if code<=6:value=int.from_bytes(data,'big',signed=True)
                    elif code==7:value=struct.unpack('>d',data)[0]
                    elif code&1:value=data.decode(self.reader.encoding,errors='replace')
                    else:value=data
                out[name]=value
            offset+=length
        return out

class TableReader:
    def row(self,table,rowid,wanted):
        """Bounded primary-key lookup including QQ's nonstandard lock page."""
        if not hasattr(self,'_layouts'):self._layouts={}
        if table not in self._layouts:
            found=next(self.connection.execute("SELECT rootpage,sql FROM sqlite_master WHERE type='table' AND name=?",(table,)),None)
            if not found:return None
            if 'WITHOUT ROWID' in found[1].upper():malformed('暂不支持无 rowid 表')
            info=list(self.connection.execute(f'PRAGMA table_info("{table}")'))
            pks=[r for r in info if r[5]]
            alias=pks[0][1] if len(pks)==1 and pks[0][2].upper()=='INTEGER' else None
            self._layouts[table]=(found[0],[r[1] for r in info],alias)
        number,columns,alias=self._layouts[table];seen=set()
        for _ in range(65):
            if number in seen:malformed('树页面重复或循环')
            seen.add(number);b=self.page(number);offset=100 if number==1 else 0
            kind=b[offset];cells=int.from_bytes(b[offset+3:offset+5],'big');h=12 if kind==5 else 8
            if kind not in (5,13) or offset+h+cells*2>self.usable:malformed('表页头异常')
            pointers=[int.from_bytes(b[offset+h+2*i:offset+h+2*i+2],'big') for i in range(cells)]
            if len(set(pointers))!=len(pointers):malformed('重复的记录指针')
            child=int.from_bytes(b[offset+8:offset+12],'big') if kind==5 else None
            previous=None
            for pointer in pointers:
                if not offset+h+cells*2<=pointer<self.usable:malformed('记录指针越界')
                if kind==5:
                    key,_=varint(b[:self.usable],pointer+4);key=signed64(key)
                else:
                    size,pos=varint(b[:self.usable],pointer);key,pos=varint(b[:self.usable],pos);key=signed64(key)
                if previous is not None and key<=previous:malformed('记录顺序异常')
                previous=key
                if key<rowid:continue
                if kind==5:
                    child=int.from_bytes(b[pointer:pointer+4],'big');break
                return Payload(self,b,pos,size).fields(columns,wanted,key,alias) if key==rowid else None
            if kind==13:return None
            number=child
        malformed('树深度异常')

    def __init__(self,db,connection,cancel=None):
        self.db=db;self.connection=connection;self.cancel=cancel
        self.page_size=db.params.page_size;header=db.page(1);self.usable=self.page_size-header[20]
        self.encoding={1:'utf-8',2:'utf-16-le',3:'utf-16-be'}.get(int.from_bytes(header[56:60],'big'))
        if not self.encoding or self.usable<480:malformed('数据库头参数不受支持')
        self.used_lock_page=False
    def page(self,number):
        if self.cancel is not None and self.cancel.is_set():raise InterruptedError('已取消读取')
        if not 1<=number<=self.db.page_count:malformed('页号越界')
        if number==0x40000000//self.page_size+1:self.used_lock_page=True
        return self.db.page(number)
    def rows(self,table,wanted,after=None,count_only=False):
        row=next(self.connection.execute('SELECT rootpage,sql FROM sqlite_master WHERE type=\'table\' AND name=?',(table,)),None)
        if not row:return
        if 'WITHOUT ROWID' in row[1].upper():malformed('暂不支持无 rowid 表')
        info=list(self.connection.execute(f'PRAGMA table_info("{table}")'))
        columns=[r[1] for r in info];pks=[r for r in info if r[5]];alias=pks[0][1] if len(pks)==1 and pks[0][2].upper()=='INTEGER' else None
        seen=bytearray((self.db.page_count+8)//8);last=None
        def walk(number,depth=0):
            nonlocal last
            if depth>64:malformed('树深度异常')
            b=self.page(number)
            if seen[number//8] & (1<<(number%8)):malformed('树页面重复或循环')
            seen[number//8]|=1<<(number%8)
            offset=100 if number==1 else 0;kind=b[offset];cells=int.from_bytes(b[offset+3:offset+5],'big')
            h=12 if kind==5 else 8
            if kind not in (5,13) or offset+h+cells*2>self.usable:malformed('表页头异常')
            pointers=[int.from_bytes(b[offset+h+2*i:offset+h+2*i+2],'big') for i in range(cells)]
            if len(set(pointers))!=len(pointers):malformed('重复的记录指针')
            prev_key=None
            for pointer in pointers:
                if not offset+h+cells*2<=pointer<self.usable:malformed('记录指针越界')
                if kind==5:
                    child=int.from_bytes(b[pointer:pointer+4],'big');key,_=varint(b[:self.usable],pointer+4);key=signed64(key)
                    if prev_key is not None and key<=prev_key:malformed('树键顺序异常')
                    prev_key=key
                    if after is None or key>after:yield from walk(child,depth+1)
                else:
                    size,pos=varint(b[:self.usable],pointer);rid,pos=varint(b[:self.usable],pos);rid=signed64(rid)
                    if last is not None and rid<=last:malformed('记录顺序异常')
                    last=rid
                    if after is not None and rid<=after:continue
                    payload=Payload(self,b,pos,size)
                    yield rid,({} if count_only else payload.fields(columns,wanted,rid,alias))
            if kind==5:yield from walk(int.from_bytes(b[offset+8:offset+12],'big'),depth+1)
        yield from walk(row[0])
