import os
import platform
import threading
from abc import ABC, abstractmethod

# This is a list to keep track of who we've blocked
BLOCKED_IP_CACHE = set()

# --- ADDED START: persistent blocked list ---
import json
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BLOCK_FILE = os.path.join(BASE_DIR, "controller_logs", "blocked.json")
os.makedirs(os.path.join(BASE_DIR, "controller_logs"), exist_ok=True)

# Load previously blocked IPs if file exists
if os.path.exists(BLOCK_FILE):
    try:
        with open(BLOCK_FILE, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                BLOCKED_IP_CACHE.update(data)
    except Exception:
        pass

def save_blocked_cache():
    try:
        with open(BLOCK_FILE, "w") as f:
            json.dump(list(BLOCKED_IP_CACHE), f)
    except Exception:
        pass
# --- ADDED END ---

# --- Abstract Base Class (The "Standard" Way) ---
# This defines a "contract" for what a Countermeasure must do.
class BaseCountermeasure(ABC):
    
    @abstractmethod
    def block_ip(self, ip_address, duration):
        """Blocks an IP for a specific duration."""
        pass

    @abstractmethod
    def unblock_ip(self, ip_address):
        """Removes the block for an IP."""
        pass

# --- Windows Implementation ---
class WindowsFirewall(BaseCountermeasure):
    
    def block_ip(self, ip_address, duration):
        if ip_address in BLOCKED_IP_CACHE:
            return f"IP_ALREADY_BLOCKED_FOR_{duration}s"
            
        print(f"[Countermeasure] Blocking {ip_address} for {duration}s...")
        rule_name = f"Block_SDN_Attacker_{ip_address}"
        
        cmd = f'netsh advfirewall firewall add rule name="{rule_name}" dir=in interface=any action=block remoteip={ip_address}'
        
        try:
            os.system(cmd)
            BLOCKED_IP_CACHE.add(ip_address)
            # --- ADDED: Save persistently ---
            save_blocked_cache()
            # --- END ADD ---
            
            # THIS IS THE UPGRADE: Schedule the unblock
            t = threading.Timer(duration, self.unblock_ip, args=[ip_address])
            t.daemon = True # Allows program to exit even if timer is running
            t.start()
            
            return f"IP_BLOCKED_FOR_{duration}s"
        except Exception as e:
            print(f"[Countermeasure] ERROR: Could not block IP. {e}")
            return "BLOCK_FAILED"

    def unblock_ip(self, ip_address):
        if ip_address not in BLOCKED_IP_CACHE:
            return # Already unblocked
            
        print(f"[Countermeasure] Unblocking {ip_address} (duration expired).")
        rule_name = f"Block_SDN_Attacker_{ip_address}"
        cmd = f'netsh advfirewall firewall delete rule name="{rule_name}"'
        
        try:
            os.system(cmd)
            BLOCKED_IP_CACHE.remove(ip_address)
            # --- ADDED: Save persistently ---
            save_blocked_cache()
            # --- END ADD ---
        except Exception as e:
            print(f"[Countermeasure] ERROR: Could not unblock IP. {e}")

# --- Linux Implementation (for reference) ---
class LinuxIPTables(BaseCountermeasure):
    def block_ip(self, ip_address, duration):
        if ip_address in BLOCKED_IP_CACHE:
            return f"IP_ALREADY_BLOCKED_FOR_{duration}s"
        
        # This is complex, as 'iptables' doesn't have timed rules.
        # A common way is to use 'at' or a helper process.
        # For simplicity, we'll just block.
        cmd = f'iptables -A INPUT -s {ip_address} -j DROP'
        os.system(cmd)
        BLOCKED_IP_CACHE.add(ip_address)
        # --- ADDED ---
        save_blocked_cache()
        # --- END ADD ---
        return f"IP_BLOCKED_FOR_{duration}s"

    def unblock_ip(self, ip_address):
        cmd = f'iptables -D INPUT -s {ip_address} -j DROP'
        os.system(cmd)
        BLOCKED_IP_CACHE.remove(ip_address)
        # --- ADDED ---
        save_blocked_cache()
        # --- END ADD ---
        
# --- SDN Implementation (Stub) ---
class SDNController(BaseCountermeasure):
    def block_ip(self, ip_address, duration):
        print(f"[Countermeasure-SDN] Pushing flow rule to SDN Controller to drop {ip_address}...")
        # --- ADDED START: optional REST API call ---
        import requests
        SDN_CONTROLLER_URL = "http://127.0.0.1:8080/stats/flowentry/add"
        payload = {
            "dpid": 1,
            "priority": 1000,
            "match": {"ipv4_src": ip_address, "eth_type": 2048},
            "actions": []
        }
        try:
            requests.post(SDN_CONTROLLER_URL, json=payload, timeout=3)
        except Exception:
            pass
        # --- ADDED END ---
        BLOCKED_IP_CACHE.add(ip_address)
        save_blocked_cache()
        
        # Also schedule an unblock
        t = threading.Timer(duration, self.unblock_ip, args=[ip_address])
        t.start()
        
        return f"SDN_BLOCK_PUSHED_FOR_{duration}s"

    def unblock_ip(self, ip_address):
        print(f"[Countermeasure-SDN] Pushing flow rule to SDN Controller to delete block for {ip_address}...")
        BLOCKED_IP_CACHE.remove(ip_address)
        save_blocked_cache()
