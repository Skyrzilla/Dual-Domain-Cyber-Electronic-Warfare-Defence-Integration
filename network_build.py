# network_build.py
import socket

def get_host_ip():
    """
    Finds the computer's local IPv4 address.
    """
    s = None
    try:
        # Connect to a public DNS server (doesn't send data)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception as e:
        print(f"Error finding IP: {e}")
        ip = '127.0.0.1' # Fallback to loopback
    finally:
        if s:
            s.close()
    return ip

if __name__ == "__main__":
    # To test this file, just run: python network_build.py
    print(f"Host IP Address: {get_host_ip()}")
