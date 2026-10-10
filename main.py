from qqarchive.app import main
if __name__=='__main__':
    from multiprocessing import freeze_support
    freeze_support()
    raise SystemExit(main())
