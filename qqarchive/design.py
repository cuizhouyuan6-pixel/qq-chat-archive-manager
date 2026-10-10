"""Shared desktop design tokens and motion preference."""
import os
TOKENS=dict(background='#F8F9FB',panel='#FFFFFF',ink='#202126',primary='#24262C',
            secondary='#6E7480',muted='#868990',line='#E9EAED',hover='#F4F5F7',
            success='#39765A',error='#A54A50',radius=12,dialog_radius=16,
            spacing=(4,8,12,16,24,32),feedback_ms=150,popup_ms=190,expand_ms=220)

def motion_ms(value):
    if os.environ.get('QQARCHIVE_REDUCED_MOTION')=='1':return 0
    if os.name=='nt':
        try:
            import ctypes
            enabled=ctypes.c_int(1)
            if ctypes.windll.user32.SystemParametersInfoW(0x1042,0,ctypes.byref(enabled),0) and not enabled.value:return 0
        except (OSError,AttributeError):pass
    return int(value)
