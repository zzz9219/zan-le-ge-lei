"""Hidden scheduled runner; progress/errors stay local instead of opening a console."""
from contextlib import redirect_stdout,redirect_stderr
import sys
from library import ROOT,RUNTIME,today,main


if __name__=='__main__':
    folder=RUNTIME/'logs'
    folder.mkdir(parents=True,exist_ok=True)
    with (folder/(today()+'.log')).open('a',encoding='utf-8') as log:
        sys.argv=[sys.argv[0],'daily']
        with redirect_stdout(log),redirect_stderr(log):
            code=main()
    raise SystemExit(code)
