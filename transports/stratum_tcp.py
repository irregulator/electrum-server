import json
import Queue as queue
import socket
import select
import threading
import time
import traceback, sys

from processor import Session, Dispatcher
from utils import print_log


READ_ONLY = select.POLLIN | select.POLLPRI | select.POLLHUP | select.POLLERR
READ_WRITE = READ_ONLY | select.POLLOUT
TIMEOUT = 100

import ssl

class TcpSession(Session):

    def __init__(self, dispatcher, poller, connection, address, use_ssl, ssl_certfile, ssl_keyfile):
        Session.__init__(self, dispatcher)
        self.use_ssl = use_ssl
        self.poller = poller
        self.raw_connection = connection
        if use_ssl:
            import ssl
            self._connection = ssl.wrap_socket(
                connection,
                server_side=True,
                certfile=ssl_certfile,
                keyfile=ssl_keyfile,
                ssl_version=ssl.PROTOCOL_SSLv23,
                do_handshake_on_connect=False)
        else:
            self._connection = connection

        self.address = address[0] + ":%d"%address[1]
        self.name = "TCP " if not use_ssl else "SSL "
        self.timeout = 1000
        self.dispatcher.add_session(self)
        self.response_queue = queue.Queue()
        self.message = ''
        self.retry_msg = ''
        self.handshake = not self.use_ssl


    def check_do_handshake(self):
        if self.handshake:
            return
        try:
            self._connection.do_handshake()
        except ssl.SSLError as err:
            if err.args[0] == ssl.SSL_ERROR_WANT_READ:
                return
            elif err.args[0] == ssl.SSL_ERROR_WANT_WRITE:
                self.poller.modify(self.raw_connection, READ_WRITE)
                return
            else:
                raise

        self.poller.modify(self.raw_connection, READ_ONLY)
        self.handshake = True



    def connection(self):
        if self.stopped():
            raise Exception("Session was stopped")
        else:
            return self._connection

    def shutdown(self):
        try:
            self._connection.shutdown(socket.SHUT_RDWR)
        except:
            # print_log("problem shutting down", self.address)
            # traceback.print_exc(file=sys.stdout)
            pass

        self._connection.close()

    def send_response(self, response):
        try:
            msg = json.dumps(response) + '\n'
        except:
            traceback.print_exc(file=sys.stdout)
            return

        self.response_queue.put(msg)

        try:
            self.poller.modify(self.raw_connection, READ_WRITE)
        except:
            traceback.print_exc(file=sys.stdout)
            return



    def parse_message(self):

        message = self.message
        self.time = time.time()

        raw_buffer = message.find('\n')
        if raw_buffer == -1:
            return False

        raw_command = message[0:raw_buffer].strip()
        self.message = message[raw_buffer + 1:]
        return raw_command





class TcpServer(threading.Thread):

    def __init__(self, dispatcher, host, port, use_ssl, ssl_certfile, ssl_keyfile):
        self.shared = dispatcher.shared
        self.dispatcher = dispatcher.request_dispatcher
        threading.Thread.__init__(self)
        self.daemon = True
        self.host = host
        self.port = port
        self.lock = threading.Lock()
        self.use_ssl = use_ssl
        self.ssl_keyfile = ssl_keyfile
        self.ssl_certfile = ssl_certfile

        self.fd_to_session = {}
        self.buffer_size = 4096





    def handle_command(self, raw_command, session):
        try:
            command = json.loads(raw_command)
        except:
            session.send_response({"error": "bad JSON"})
            return True

        try:
            # Try to load vital fields, and return an error if
            # unsuccessful.
            message_id = command['id']
            method = command['method']
        except KeyError:
            # Return an error JSON in response.
            session.send_response({"error": "syntax error", "request": raw_command})
        else:
            self.dispatcher.push_request(session, command)
            ## sleep a bit to prevent a single session from DOSing the queue
            #time.sleep(0.01)





    def run(self):

        print_log( ("SSL" if self.use_ssl else "TCP") + " server started on port %d"%self.port)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setblocking(0)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(5)
        server_fd = server.fileno()

        poller = select.poll()
        poller.register(server)

        def stop_session(fd):
            try:
                # unregister before we close s 
                poller.unregister(fd)
            except:
                print_log("unregister error", fd)
                traceback.print_exc(file=sys.stdout)

            session = self.fd_to_session.pop(fd)
            # this will close the socket
            session.stop()


        redo = []

        while not self.shared.stopped():
            if redo:
                events = redo
                redo = []
            else:
                events = poller.poll(TIMEOUT)

            for fd, flag in events:

                if fd != server_fd:
                    session = self.fd_to_session[fd]
                    s = session._connection
                    try:
                        session.check_do_handshake()
                    except:
                        stop_session(fd)
                        continue

                # handle inputs
                if flag & (select.POLLIN | select.POLLPRI):

                    if fd == server_fd:
                        connection, address = server.accept()
                        try:
                            session = TcpSession(self.dispatcher, poller, connection, address, 
                                                 use_ssl=self.use_ssl, ssl_certfile=self.ssl_certfile, ssl_keyfile=self.ssl_keyfile)
                        except BaseException as e:
                            print_log("cannot start TCP session", str(e), address)
                            connection.close()
                            continue

                        connection = session._connection
                        connection.setblocking(0)
                        self.fd_to_session[ connection.fileno() ] = session
                        poller.register(connection, READ_ONLY)
                        try:
                            session.check_do_handshake()
                        except:
                            print_log( "handshake failure", address )
                            stop_session(connection.fileno())

                        continue

                    try:
                        data = s.recv(self.buffer_size)
                    except ssl.SSLError as x:
                        if x.args[0] == ssl.SSL_ERROR_WANT_READ: 
                            # print_log("error want read", x, fd)
                            continue 
                        else: 
                            raise x
                    except socket.error as x:
                        # print_log("recv err", x)
                        stop_session(fd)
                        continue

                    if data:
                        if len(data) == self.buffer_size:
                            redo.append( (fd, flag) )

                        session.message += data
                        while True:
                            cmd = session.parse_message()
                            if not cmd: 
                                break
                            if cmd == 'quit':
                                data = False
                                break
                            self.handle_command(cmd, session)

                    if not data:
                        stop_session(fd)
                        continue

                elif flag & select.POLLHUP:
                    print_log('client hung up', address)
                    stop_session(fd)


                elif flag & select.POLLOUT:
                    # Socket is ready to send data, if there is any to send.
                    if session.retry_msg:
                        next_msg = session.retry_msg
                    else:
                        try:
                            next_msg = session.response_queue.get_nowait()
                        except queue.Empty:
                            # No messages waiting so stop checking for writability.
                            poller.modify(s, READ_ONLY)
                            continue

                    sent = s.send(next_msg)
                    session.retry_msg = next_msg[sent:]


                elif flag & select.POLLERR:
                    print_log('handling exceptional condition for', session.address)
                    stop_session(fd)


                elif flag & select.POLLNVAL:
                    print_log('invalid request', session.address)
                    stop_session(fd)
