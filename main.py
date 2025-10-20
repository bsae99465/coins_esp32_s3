# main.py - โค้ดหลักสำหรับระบบเครื่องขายสินค้า (Vending/POS)
# พัฒนาโดย บอส (Boss) - ทีมโปรแกรมเมอร์ API & IOT
# ใช้ uasyncio สำหรับการทำงานแบบ Non-blocking

import time
import asyncio
from machine import Pin, freq
from hardware_config import *

# ต้องมีไฟล์ tm1637.py ในบอร์ดของคุณ
try:
    from tm1637 import TM1637
except ImportError:
    print("❌ Error: tm1637.py library not found. Display function will be skipped.")
    TM1637 = None

# --- Global State Variables (ควบคุมสถานะระบบ) ---
# ใช้ volatile ในภาษา C, แต่ใน MicroPython ใช้ Global/Class
total_credit = 0            # เครดิตรวมที่มีอยู่ในระบบ (บาท)
pulse_count_in_ISR = 0      # ตัวนับพัลส์จากเครื่องรับธนบัตร/เหรียญ (Input)
payout_pulse_count_ISR = 0  # ตัวนับพัลส์จาก Coin Hopper (Output Feedback)
target_payout_pulses = 0    # เป้าหมายจำนวนเหรียญที่จะต้องจ่าย
payout_in_progress = False  # สถานะ: กำลังจ่ายเหรียญหรือไม่
last_displayed_credit = -1  # เครดิตที่แสดงบนจอครั้งล่าสุด
system_log = []             # สำหรับเก็บ Log เหตุการณ์สำคัญ

# --- Hardware Initialization ---

# 1. Bill/Coin Acceptor Input (ใช้ PULL_UP และ Interrupt)
bill_pin = Pin(BILL_ACCEPTOR_PIN, Pin.IN, Pin.PULL_UP)

# 2. Coin Hopper Payout Feedback Input (ใช้ PULL_UP และ Interrupt)
hopper_feedback_pin = Pin(HOPPER_FEEDBACK_PIN, Pin.IN, Pin.PULL_UP)

# 3. Coin Hopper Control Output (ตั้งค่าเริ่มต้นเป็น OFF)
hopper_control_pin = Pin(HOPPER_CONTROL_PIN, Pin.OUT)
hopper_control_pin.value(RELAY_OFF)

# 4. TM1637 Display Initialization
display = None
if TM1637:
    try:
        display = TM1637(clk=Pin(TM1637_CLK_PIN), dio=Pin(TM1637_DIO_PIN))
        display.brightness(2) 
    except Exception as e:
        print(f"❌ Failed to initialize TM1637: {e}")
        display = None

# --- Interrupt Service Routines (ISRs) ---

def bill_acceptor_isr(pin):
    """ISR: นั่งฟังสัญญาณพัลส์จากเครื่องรับธนบัตร/เหรียญเข้า"""
    global pulse_count_in_ISR
    # เพิ่มตัวนับเมื่อตรวจพบสัญญาณ (FALLING edge)
    pulse_count_in_ISR += 1

def hopper_feedback_isr(pin):
    """ISR: นั่งฟังสัญญาณพัลส์จาก Coin Hopper เมื่อจ่ายเหรียญออกไป"""
    global payout_pulse_count_ISR
    # เพิ่มตัวนับเมื่อเหรียญผ่านเซนเซอร์ (FALLING edge)
    payout_pulse_count_ISR += 1


# --- Attach Interrupts ---

bill_pin.irq(trigger=Pin.IRQ_FALLING, handler=bill_acceptor_isr)
hopper_feedback_pin.irq(trigger=Pin.IRQ_FALLING, handler=hopper_feedback_isr)


# --- Core Logic Functions (ใช้ uasyncio task) ---

async def handle_bill_credit():
    """Task: ตรวจสอบและประมวลผลเครดิตที่ได้รับจากธนบัตร/เหรียญ"""
    global pulse_count_in_ISR
    global total_credit
    global system_log
    
    # ตรวจสอบพัลส์เป็นรอบๆ เพื่อลดเวลาใน ISR
    while True:
        await asyncio.sleep_ms(100) # ตรวจสอบทุก 100ms
        
        # ดึงค่าพัลส์ที่นับได้จาก ISR มาประมวลผลและรีเซ็ต
        pulses = pulse_count_in_ISR 
        pulse_count_in_ISR = 0 
        
        if pulses > 0:
            credit_added = pulses * PULSE_PER_UNIT_VALUE
            total_credit += credit_added
            
            log_msg = f"✅ Credit: +{credit_added} THB. Total: {total_credit} THB"
            system_log.append(log_msg)
            print(log_msg)
            
            # --- API/MQTT Logic (สำหรับคุณบอส) ---
            # TODO: เพิ่มโค้ด Publish ข้อมูลเครดิตใหม่ไปยัง MQTT Server
            # self.publish_to_mqtt("credit/update", str(credit_added))
            # ---------------------------------------------------------

async def monitor_payout():
    """Task: ควบคุมและตรวจสอบสถานะการจ่ายเหรียญจนเสร็จสิ้น"""
    global payout_in_progress
    global payout_pulse_count_ISR
    global target_payout_pulses
    global total_credit
    global system_log

    while True:
        await asyncio.sleep_ms(50) # ตรวจสอบถี่ขึ้นเมื่อกำลังจ่ายเหรียญ
        
        if not payout_in_progress:
            continue

        # ลอจิกการหยุดจ่ายเหรียญ (ถ้าจำนวนพัลส์ถึงเป้าหมาย)
        if payout_pulse_count_ISR >= target_payout_pulses:
            
            # 1. หยุดมอเตอร์ Hopper
            hopper_control_pin.value(RELAY_OFF)
            payout_in_progress = False
            
            # 2. อัพเดทเครดิต
            total_credit -= target_payout_pulses # หักลบตามจำนวนเหรียญที่จ่ายออกไป
            
            log_msg = f"✅ Payout Complete! Paid: {target_payout_pulses} coins. Remaining: {total_credit} THB"
            system_log.append(log_msg)
            print(log_msg)
            
            # --- API/MQTT Logic ---
            # TODO: เพิ่มโค้ดแจ้งสถานะการจ่ายเงินไปยัง Server
            # self.publish_to_mqtt("payout/status", "success")
            # ----------------------
            
        else:
            # ยังไม่ครบจำนวน
            # print(f"   Paying out... {payout_pulse_count_ISR}/{target_payout_pulses}", end='\r')
            pass # ลดการพิมพ์ในลูปเพื่อประสิทธิภาพ


async def display_manager():
    """Task: จัดการการแสดงผลบนจอ TM1637"""
    global total_credit
    global last_displayed_credit
    
    if not display:
        return

    while True:
        # อัปเดตจอเฉพาะเมื่อค่าเครดิตมีการเปลี่ยนแปลง
        if total_credit != last_displayed_credit:
            # ตรวจสอบไม่ให้ตัวเลขเกิน 4 หลัก (9999)
            display_value = min(total_credit, 9999) 
            
            # แสดงผลตัวเลข 
            display.show(display_value) 
            last_displayed_credit = total_credit
            # print(f"📺 Display updated: {display_value}")

        await asyncio.sleep_ms(500) # อัปเดตทุก 0.5 วินาที


# --- Dummy Test/Command Function ---

def start_payout(amount_to_pay_out):
    """ฟังก์ชันจำลอง: เริ่มกระบวนการจ่ายเหรียญ (จะถูกเรียกจาก MQTT command ในระบบจริง)"""
    global total_credit
    global payout_in_progress
    global target_payout_pulses
    global payout_pulse_count_ISR
    
    if payout_in_progress:
        print("❌ Payout is already in progress.")
        return False

    if amount_to_pay_out > total_credit:
        print("❌ Insufficient credit for payout.")
        return False
        
    target_payout_pulses = amount_to_pay_out 
    payout_pulse_count_ISR = 0 
    payout_in_progress = True
    
    print(f"💰 Starting payout: {target_payout_pulses} coins...")
    
    # เปิดมอเตอร์ Hopper (Active Low)
    hopper_control_pin.value(RELAY_ON) 
    
    return True

# --- Main Application Runner ---

async def main_application():
    """ฟังก์ชันหลักสำหรับรวม Task ทั้งหมดเข้าด้วยกัน"""
    
    # 1. Task IOT/MQTT Client (คุณบอสต้องเพิ่มโค้ดในส่วนนี้)
    # asyncio.create_task(mqtt_client_task()) 
    
    # 2. Task ตรวจสอบเครดิตเข้า
    asyncio.create_task(handle_bill_credit())
    
    # 3. Task ควบคุมการจ่ายเหรียญ
    asyncio.create_task(monitor_payout())

    # 4. Task จัดการจอแสดงผล
    if display:
        asyncio.create_task(display_manager())
        display.scroll("INIT")
        await asyncio.sleep(2) # หน่วงเวลาให้ Scroll เสร็จ
    
    print("System Running: Awaiting Input and MQTT Commands...")

    # --- Dummy Payout Test (ลบออกเมื่อใช้ MQTT) ---
    await asyncio.sleep(5)
    # start_payout(20) # ทดสอบจ่ายเหรียญ 20 บาท ถ้ามีเครดิต
    # --------------------------------------------
    
    # Loop หลักสำหรับรันระบบไปเรื่อยๆ
    while True:
        await asyncio.sleep(1) 


# --- Start of Program ---
if __name__ == "__main__":
    try:
        # กำหนดความถี่ CPU (ถ้าจำเป็น)
        # freq(240000000) 
        asyncio.run(main_application())
    except KeyboardInterrupt:
        print("\nSystem Halted by User.")
    finally:
        # ล้างสถานะเมื่อจบโปรแกรม
        print("Cleaning up hardware...")
        if display:
            display.show("    ") # เคลียร์จอ
        hopper_control_pin.value(RELAY_OFF)
        asyncio.new_event_loop() # เคลียร์ asyncio loop

