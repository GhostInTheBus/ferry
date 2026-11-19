"""Throwaway SFTP + FTP servers for end to end tests."""
import asyncio
import os
import sys

import aioftp
import asyncssh

BASE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/ferrytest"
SFTP_ROOT = os.path.join(BASE, "sftp")
FTP_ROOT = os.path.join(BASE, "ftp")
FERRY_PUB = os.path.join(BASE, "data", "ssh", "id_ed25519.pub")


class Server(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == "demo" and password == "sftp-pw"


async def main():
    os.makedirs(SFTP_ROOT, exist_ok=True)
    os.makedirs(FTP_ROOT, exist_ok=True)
    hk = os.path.join(BASE, "hostkey")
    if not os.path.exists(hk):
        asyncssh.generate_private_key("ssh-ed25519").write_private_key(hk)
    while not os.path.exists(FERRY_PUB):
        await asyncio.sleep(0.2)
    await asyncssh.create_server(
        Server, "127.0.0.1", 2222, server_host_keys=[hk],
        authorized_client_keys=FERRY_PUB,
        sftp_factory=lambda chan: asyncssh.SFTPServer(chan, chroot=SFTP_ROOT.encode()))
    users = [aioftp.User("ftpuser", "ftp-pw", base_path=FTP_ROOT, home_path="/",
                         permissions=[aioftp.Permission("/", readable=True, writable=True)])]
    ftp = aioftp.Server(users)
    await ftp.start("127.0.0.1", 2121)
    print("servers up", flush=True)
    await asyncio.Event().wait()


asyncio.run(main())
