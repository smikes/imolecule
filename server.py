"""
A server to communicate with the javascript-based viewer.

Currently implements zeroMQ sockets server side, which are mapped to javascript
websockets wrapped with SocketIO using tornado.
    * zeroMQ - A wrapper around sockets that handles a lot of messiness
               involved with network connections
    * socketIO - A wrapper around javascript websockets that handles the
                 differences in implementations across browser/OS combinations
    * tornado - A Python-based web framework that allows us to convert easily
                between the zeroMQ and socketIO wrappers.
It sounds complicated to use all of these libraries, but it makes this approach
more robust and surprisingly easier.
"""
import os
import multiprocessing
import logging
import traceback

import zmq
from zmq.eventloop import ioloop, zmqstream
ioloop.install()

import tornado
import tornado.web
import tornadio
import tornadio.router
import tornadio.server


def _worker_process(msg):
    """Wrapper function for worker process to execute."""
    import format_converter
    return getattr(format_converter, msg["method"])(**msg["params"])


class IndexHandler(tornado.web.RequestHandler):

    def get(self):
        self.write(INDEX)


class ClientConnection(tornadio.SocketConnection):

    def __init__(self, *args, **kwargs):
        self.pool = multiprocessing.Pool(processes=WORKERS)
        super(ClientConnection, self).__init__(*args, **kwargs)

    def on_message(self, message):
        """Evaluates the function pointed to by json-rpc."""
        logging.log(logging.DEBUG, message)

        # Spawn a process to protect the server against error signals
        async = self.pool.apply_async(_worker_process, [message])
        try:
            result = async.get(timeout=TIMEOUT)
            error = 0
        except multiprocessing.TimeoutError:
            result = ("File format conversion timed out! This is due "
                      "either to a large input file or a segmentation "
                      "fault in the underlying open babel library.")
            error = 1
            self.pool.terminate()
            self.pool = multiprocessing.Pool(processes=WORKERS)
        except Exception:
            result = traceback.format_exc()
            error = 1
        logging.log(logging.DEBUG, result)
        self.send({"result": result, "error": error, "id": message["id"]})


if __name__ == "__main__":
    import argparse
    import webbrowser

    ROOT = os.path.normpath(os.path.dirname(__file__))

    parser = argparse.ArgumentParser(description="Opens a browser-based "
                                     "client that interfaces with the chemical"
                                     " format converter.")
    parser.add_argument("--debug", action="store_true", help="Prints all "
                        "transmitted data streams")
    parser.add_argument("--http-port", type=int, default=8000, help="The port "
                        "on which to serve the website")
    parser.add_argument("--tcp-port", type=int, default=5000, help="The "
                        "server-side tcp connection for python-js interaction")
    parser.add_argument("--timeout", type=int, default=5, help="The maximum "
                        "time, in seconds, allowed for a process to run "
                        "before returning an error")
    parser.add_argument("--workers", type=int, default=2, help="The number of "
                        "worker processes to use with the server.")
    parser.add_argument("--no-browser", action="store_true", help="Disables "
                        "opening a browser window on startup.")
    args = parser.parse_args()

    HTTP_PORT, TCP_PORT = args.http_port, args.tcp_port
    TIMEOUT, WORKERS = args.timeout, args.workers

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    with open(os.path.join(ROOT, "index.html")) as index_file:
        INDEX = index_file.read() % {"port": HTTP_PORT}

    WebClientRouter = tornadio.get_router(ClientConnection)
    handler = [(r"/", IndexHandler), WebClientRouter.route(),
               (r'/static/(.*)', tornado.web.StaticFileHandler,
                {'path': ROOT})]
    kwargs = {"enabled_protocols": ["websocket"],
              "socket_io_port": HTTP_PORT}
    application = tornado.web.Application(handler, **kwargs)

    if not args.no_browser:
        webbrowser.open("http://localhost:%d/" % HTTP_PORT, new=2)

    context = zmq.Context()
    socket = context.socket(zmq.REP)
    socket.bind("tcp://127.0.0.1:%d" % TCP_PORT)
    stream = zmqstream.ZMQStream(socket, tornado.ioloop.IOLoop.instance())
    stream.on_recv(ClientConnection.on_message)
    tornadio.server.SocketServer(application)
