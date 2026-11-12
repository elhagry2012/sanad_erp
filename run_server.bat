@echo off
cd /d H:\sanad_erp
C:\Users\Dell\AppData\Local\Programs\Python\Python311\python.exe -m tools.pwa_setup
C:\Users\Dell\AppData\Local\Programs\Python\Python311\python.exe -m streamlit run app.py --server.headless=true