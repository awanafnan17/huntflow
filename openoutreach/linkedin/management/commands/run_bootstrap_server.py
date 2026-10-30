# openoutreach/linkedin/management/commands/run_bootstrap_server.py
"""
Django management command to run the dedicated HuntFlow bootstrap runtime server.

Usage:
    python manage.py run_bootstrap_server --host 0.0.0.0 --port 8001
"""
import asyncio
import logging
import signal
from django.core.management.base import BaseCommand
from openoutreach.linkedin.services.bootstrap_runtime import BootstrapServer

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Runs the dedicated HuntFlow server bootstrap WebSocket and screencast server."

    def add_arguments(self, parser):
        parser.add_argument(
            "--host",
            default="0.0.0.0",
            help="Host interface to bind on (default: 0.0.0.0)",
        )
        parser.add_argument(
            "--port",
            type=int,
            default=8001,
            help="Port to listen on (default: 8001)",
        )

    def handle(self, *args, **options):
        host = options["host"]
        port = options["port"]

        self.stdout.write(self.style.SUCCESS(f"Starting HuntFlow Bootstrap Server on {host}:{port}..."))
        
        async def run_server():
            server = BootstrapServer(host=host, port=port)
            await server.start()

            stop_event = asyncio.Event()

            def _handle_signal(*_):
                logger.info("Termination signal received. Stopping bootstrap server...")
                stop_event.set()

            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, _handle_signal)
                except NotImplementedError:
                    pass

            await stop_event.wait()
            await server.stop()
            self.stdout.write(self.style.SUCCESS("Bootstrap server stopped gracefully."))

        try:
            asyncio.run(run_server())
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Bootstrap server interrupted by operator."))
