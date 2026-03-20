import argparse
import os
from pathlib import Path

from pipeline.cli_runner import run_pipeline_from_cli
from utils.logger import get_logger

logger = get_logger(__name__)

try:
    from flask import Flask, jsonify, request
except ImportError:
    Flask = None
    jsonify = None
    request = None

try:
    from line.line_webhook import handle_line_webhook
except ImportError as exc:
    logger.warning("LINE webhook handler import failed: %s", exc)
    handle_line_webhook = None


def _create_app():
    if Flask is None:
        raise RuntimeError("Flask is not installed.")

    app = Flask(__name__)

    @app.get("/health")
    def health_check():
        return {"status": "ok"}

    @app.post("/callback")
    @app.post("/line/webhook")
    def line_webhook():
        if handle_line_webhook is None:
            return {"error": "LINE webhook handler is not available."}, 501

        try:
            body = request.get_data(as_text=True)
            signature = request.headers.get("X-Line-Signature", "")
            response = handle_line_webhook(body=body, signature=signature)

            if isinstance(response, tuple):
                return response

            if isinstance(response, dict):
                return jsonify(response)

            return {"status": "ok"}
        except Exception as exc:
            logger.exception(f"LINE webhook handling failed: {exc}")
            return {"error": "internal_server_error"}, 500

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Meeting Minutes AI")
    parser.add_argument("--audio", dest="audio_file_path", help="Path to input audio file")
    parser.add_argument(
        "--serve-line",
        action="store_true",
        help="Run LINE webhook server",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("HOST", "0.0.0.0"),
        help="Host for webhook server",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", "8000")),
        help="Port for webhook server",
    )

    args = parser.parse_args()

    if args.audio_file_path:
        audio_file_path = args.audio_file_path
        if not os.path.isabs(audio_file_path):
            project_root = os.path.dirname(os.path.abspath(__file__))
            audio_file_path = os.path.normpath(os.path.join(project_root, audio_file_path))

        run_pipeline_from_cli(audio_file_path, auto_selected_audio=False)
        return

    if args.serve_line:
        app = _create_app()
        logger.info("WEBHOOK_SERVER_START: host=%s port=%s routes=/callback,/line/webhook", args.host, args.port)
        app.run(host=args.host, port=args.port)
        return

    project_root = Path(__file__).resolve().parent
    audio_dir = project_root / "audio"
    latest_audio_file = None
    if audio_dir.exists() and audio_dir.is_dir():
        m4a_files = [p for p in audio_dir.glob("*.m4a") if p.is_file()]
        if m4a_files:
            latest_audio_file = max(m4a_files, key=lambda p: p.stat().st_mtime)

    if latest_audio_file is not None:
        logger.info("No --audio provided. Use latest audio file: %s", latest_audio_file)
        run_pipeline_from_cli(str(latest_audio_file), auto_selected_audio=True)
        return

    parser.print_help()

if __name__ == "__main__":
    main()