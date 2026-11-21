# วิธีเริ่มใช้ Trading Backtest Workstation (Prototype v0.1)

แอปนี้เป็นเครื่องมือจำลองเทรดย้อนหลังในเครื่อง ไม่ส่งคำสั่งซื้อขายจริง

## ติดตั้งใน WSL อันที่ 2

```bash
mkdir -p ~/code
cd ~/code
git clone https://github.com/junelufy/pythontest.git trading-backtest
cd trading-backtest
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
python -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501 --server.headless true
```

เปิดเบราว์เซอร์เองไปที่ http://127.0.0.1:8501

## วิธีทดลอง

1. อัปโหลดไฟล์ Lighter หรือ Binance ZIP ที่มี `candles` SQLite
2. เลือกสินทรัพย์ เช่น `XAU` แล้วตั้ง alias; กด **Load asset**
3. เลือก `PAXG` แล้วกด **Load asset** อีกครั้ง
4. ในกราฟ Synthetic ใช้สูตร `XAU - PAXG`
5. เดินเวลาไปยังแท่งที่ต้องการ กด SELL XAU และ BUY PAXG แล้วดู Floating P/L
6. ใช้ Close Position ปิดทั้งไม้หรือบางส่วน
7. Export Scenario JSON เพื่อเก็บผล

## ข้อจำกัดที่ต้องทราบ

- Market ใช้ราคาปิดของแท่งล่าสุดที่แสดงแทนราคา Fill จริง
- Limit ใช้ High/Low ของแท่งถัดไปสมมติการ Fill (ไม่มีคิว Maker)
- ไม่รองรับ Funding, Bid/Ask Depth, Margin, Liquidation, Options หรืออัตราแลกเปลี่ยนต่างสกุล
- ยังไม่มี Play อัตโนมัติและระบบ Scenario Restore; ใช้ Step และ Slider ก่อน
- ไม่ควรใช้ตัวเลขกำไรของรุ่นนี้เพื่อยืนยันว่า Strategy เทรดจริงทำกำไรได้

มีเอกสารส่งต่องานให้ Codex ที่ `AGENTS.md` และ `docs/HANDOFF.md`。
