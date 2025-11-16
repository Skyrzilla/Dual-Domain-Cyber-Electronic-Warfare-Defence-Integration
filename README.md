# Dual-Domain Cyber & RF Defence Framework Prototype

A modular defence framework prototype integrating real-time **cyber intrusion detection** with **RF/EW anomaly visualization**. The system demonstrates how cross-domain monitoring can be implemented using lightweight, scalable components suitable for research and educational use.

---

## 📌 Overview

This prototype monitors a web application for common cyber attacks (SQL Injection, XSS, Command Injection, Directory Traversal, Port Scans, Recon, SYN Floods) while applying automated IP blocking and generating structured alert logs.  
In parallel, it simulates RF/EW threats (Jamming, Spoofing, Intercept, RF MITM) using FFT-based spectrum and waterfall generation, visualized through a dedicated web dashboard.

All components run through a unified controller that launches the website, detectors, countermeasure engine, RF simulator, and monitoring dashboards together.

---

## 🚀 Features

- **Cyber Intrusion Detection**
  - System-based & log-based analysis  
  - SQLi, XSS, Command Injection, Directory Traversal, Recon, Port Scan, SYN Flood  
  - Severity-based alerting  
  - Structured logging with timestamps and IP metadata  

- **Automated Countermeasures**
  - Temporary firewall-level IP blocking  
  - Attack caching to prevent repeated triggers  
  - SDN-compatible design  

- **RF/EW Simulation & Visualization**
  - FFT-based power spectrum generation  
  - Waterfall visualization  
  - Modes: Normal, Jamming, Spoofing, Intercept, RF MITM  
  - Real-time event logging  

- **Unified Run Controller**
  - Launches all modules in a single workflow  
  - Verifies network configuration  
  - Ensures synchronized operation across components  

- **Dashboards**
  - Terminal-based cyber monitoring dashboard  
  - Web-based RF/EW spectrum dashboard  


