"""Chroma 본문을 바꾸지 않고 유사 검색용 제목 색인만 생성한다."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.sources.chroma_titles import build_title_projection


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--target',type=Path,required=True)
    args=parser.parse_args()
    result=build_title_projection(args.source,args.target)
    print(json.dumps(result,ensure_ascii=False))
    return 0 if result['status'] in {'current','built'} else 2


if __name__=='__main__':
    raise SystemExit(main())
