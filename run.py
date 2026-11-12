import argparse
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description='PaperFlow 文档图像数字化工作台')
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '8888')))
    parser.add_argument('--host', default=os.environ.get('PAPERFLOW_HOST', '127.0.0.1'))
    parser.add_argument('--no-demo', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口必须为1–65535')
    probe = socket.socket()
    try:
        probe.bind((args.host, args.port))
    except OSError as error:
        print(f'端口 {args.port} 无法使用：{error}。请用 --port 8889 指定其他端口。', file=sys.stderr)
        return 2
    finally:
        probe.close()
    from paperflow.web import create_app
    app = create_app(demo=not args.no_demo)
    print(f'PaperFlow 已启动：http://127.0.0.1:{args.port}', flush=True)
    try:
        from waitress import serve
        serve(app, host=args.host, port=args.port, threads=8, max_request_body_size=251658240)
    except ImportError:
        app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
