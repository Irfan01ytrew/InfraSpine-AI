"""Standalone regression run through project_file_run_python inside Ubuntu WSL."""
import os
import resource
import socket
import subprocess


def main():
    for forbidden in ("/mnt/c", "/mnt/d", "/init", "/var/run/docker.sock"):
        if os.path.exists(forbidden):
            raise RuntimeError(f"Unexpected host resource: {forbidden}")
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise RuntimeError("Unexpected network interface")
    if resource.getrlimit(resource.RLIMIT_CPU)[0] != 10:
        raise RuntimeError("Missing CPU limit")
    subprocess.run(["git", "init", "--quiet", "/tmp/probe"], check=True)
    subprocess.run(["git", "-C", "/tmp/probe", "status", "--porcelain"], check=True)
    print("Isolated Git workspace and resource checks passed.")


if __name__ == "__main__":
    main()
