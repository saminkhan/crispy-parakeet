#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import zmq

class Client:
    def __init__(self, ip='127.0.0.1', port=8000, datasetPath=None, compressionLevel=0,
                 recv_timeout_ms=None):
        # [longtail] This used to hardcode "tcp://127.0.0.1:8000", silently ignoring
        # both ip and port -- the mirror of the same bug on the server side. It fails
        # in the worst way: zmq connect() is non-blocking, so a wrong endpoint does not
        # raise; the first recv() simply blocks forever.
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PAIR)
        if recv_timeout_ms is not None:
            self.socket.setsockopt(zmq.RCVTIMEO, int(recv_timeout_ms))
        self.endpoint = "tcp://%s:%d" % (ip, int(port))
        self.socket.connect(self.endpoint)

    def sendMessage(self, message):
        jsonstr = message.to_json()
        self.socket.send_string(jsonstr)
        return True

    def recvMessage(self):
        frame = self.socket.recv()
        data = self.socket.recv_string()
        message = json.loads(data)
        message['frame'] = frame
        return message

    def close(self):
        self.socket.close()










