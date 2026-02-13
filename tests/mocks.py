import time

class MockCommands:
    def __init__(self):
        self.sent = []

    async def send_msg(self, destination, payload):
        # simulate network send delay
        self.sent.append((destination, payload))


class MockMesh:
    def __init__(self):
        self.commands = MockCommands()
        self.subscriptions = {}

    @classmethod
    async def create_serial(cls, device=None, baud=None):
        return cls()

    @classmethod
    async def create_tcp(cls, host=None, port=None):
        return cls()

    def subscribe(self, event, callback):
        self.subscriptions.setdefault(event, []).append(callback)

    async def close(self):
        return True


class MockSender:
    def __init__(self, fobj, packets=None):
        self.fobj = fobj
        # default to single small packet
        self.packets = packets if packets is not None else [b"MOCKDATA"]
        self._idx = 0

    def is_finished(self):
        return self._idx >= len(self.packets)

    def get_next_packet(self):
        if self._idx < len(self.packets):
            p = self.packets[self._idx]
            self._idx += 1
            return p
        return b""


class MockReceiver:
    def __init__(self, fobj):
        self.fobj = fobj
        self._finished = False

    def receive(self, data):
        # append bytes to file
        if data:
            self.fobj.write(data)
        self._finished = True

    def is_finished(self):
        return self._finished
