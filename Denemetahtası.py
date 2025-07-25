import cv2
import numpy as np
import time
import enum
import mss
import subprocess  # Knight Online'ı kapat
import pyautogui
import threading
import keyboard
import pygetwindow as gw  # Pencerelerle çalışmak için
from pynput.mouse import Controller
from main.clicksend import KeyboardDriver, MouseDriver
import pyperclip
import sys
import tkinter as tk
from tkinter import ttk, scrolledtext
from datetime import datetime
import win32gui
import win32con
import psutil

try:
    import pynput.keyboard as pynput_keyboard
except ImportError:
    print("pynput kurulu değil. 'pip install pynput' komutu ile kurun.")
    pynput_keyboard = None

mouse = Controller()


# --- Sürücüler ---
key = KeyboardDriver()
mous = MouseDriver()

# --- Model Yolları ---
YOLO_CFG = "anvil/anvl.cfg"
YOLO_WEIGHTS = "anvil/anvl_best.weights"
YOLO_NAMES = "anvil/obj.names"

# --- Model Yükle ---
net = cv2.dnn.readNetFromDarknet(YOLO_CFG, YOLO_WEIGHTS)
net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)

with open(YOLO_NAMES, "r", encoding="utf-8") as f:
    CLASSES = [c.strip() for c in f.readlines()]

# Hedef sınıflar
TORCH_CLASSES = {"sol", "sag"}
ANVIL_CLASS = "am"

MONITOR = {"top": 0, "left": 0, "width": 1920, "height": 1080}
# Ekran alanı

# Sabitler
TORCH_INNER_RATIO = 0.65
ANVIL_INNER_RATIO = 0.37
MERDIVEN_RATIO = 0.95
DOWN_RATIO = 0.6
PREDEFINED_X = 1665  # town kordinatı 1920 1080
PREDEFINED_Y = 1059
ITEM_BASMA_SURE = 0.015 #İtem basmanın süresi
ANVIL_MAX_RETRIES = 4  # anvili tarama
ANVIL_RETRY_DELAY = 1.0  # sn
POST_CLICK_ANVIL_WAIT = 5  # anvili arama süresi
LOOP_POLL = 0.25  # sn; state makinesi daha sık dönebilir
COOLDOWN_AFTER_SEQUENCE = 2.0  # sn


# ----------------- Yardımcılar -----------------
def screen_center():
    return MONITOR["width"] // 2, MONITOR["height"] // 2


def grab_screen(sct):
    # BGRA -> RGB
    img = np.array(sct.grab(MONITOR))[:, :, :3]
    return img


def detect_objects(frame, conf_threshold=0.3, nms_threshold=0.4):
    blob = cv2.dnn.blobFromImage(frame, 1 / 255.0, (416, 416), swapRB=True, crop=False)
    net.setInput(blob)
    layer_names = net.getLayerNames()
    output_layers = [layer_names[i - 1] for i in net.getUnconnectedOutLayers()]
    outputs = net.forward(output_layers)

    h, w = frame.shape[:2]
    boxes, confidences, class_ids = [], [], []

    for output in outputs:
        for det in output:
            scores = det[5:]
            class_id = np.argmax(scores)
            conf = scores[class_id]
            if conf > conf_threshold:
                cx = int(det[0] * w)
                cy = int(det[1] * h)
                bw = int(det[2] * w)
                bh = int(det[3] * h)
                x = int(cx - bw / 2)
                y = int(cy - bh / 2)
                boxes.append([x, y, bw, bh])
                confidences.append(float(conf))
                class_ids.append(class_id)

    idxs = cv2.dnn.NMSBoxes(boxes, confidences, conf_threshold, nms_threshold)
    results = []
    if len(idxs) > 0:
        for i in idxs.flatten():
            label = CLASSES[class_ids[i]]
            results.append((label, confidences[i], boxes[i]))  # (lbl, conf, [x,y,w,h])
    return results


def inward_and_down(obj_cx, obj_cy, w, h, screen_cx, ratio):
    dx = abs(obj_cx - screen_cx)
    if obj_cx > screen_cx:
        click_x = obj_cx - int(dx * ratio)
    else:
        click_x = obj_cx + int(dx * ratio)
    click_y = obj_cy + int(h * DOWN_RATIO)
    return click_x, click_y


def fixed_side_click(cx, cy, w, h):
    if cx < MONITOR["width"] // 2:
        # sol tarafta, sağ %50 içeri
        click_x = cx + w // 2 + w // 4
    else:
        # sağ tarafta, sol %50 içeri
        click_x = cx - w // 2 - w // 4
    click_y = cy + int(h * DOWN_RATIO)
    return click_x, click_y


def pick_closest_to_center(detections, allowed_labels):
    cx_screen, _ = screen_center()
    cands = []
    for label, conf, (x, y, w, h) in detections:
        if label in allowed_labels:
            obj_cx = x + w // 2
            obj_cy = y + h // 2
            dist = abs(obj_cx - cx_screen)
            cands.append((dist, label, (obj_cx, obj_cy, w, h)))
    if not cands:
        return None
    cands.sort(key=lambda t: t[0])
    return cands[0][1:]  # (label, (cx,cy,w,h))


# ----------------- Durum Tanımı -----------------
class State(enum.Enum):
    BASLANGIC = 0  # Süreç başlangıcı durumu
    CAPTURE = 1  # Görüntü yakalama durumu
    DETECT_TORCHES = 2  # Torch algılandığında yapılan durum
    CLICK_TORCH = 3  # Torch tıklama durumu
    DETECT_ANVIL = 4  # Anvil algılama durumu
    CLICK_ANVIL = 5  # Anvil'e tıklama durumu
    FALLBACK_ANVIL_COORD = 6  # Alternatif koordinatlar kullanma durumu
    COOLDOWN = 7  # Bekleme durumu
    F9BASM = 8  # Oyun içi özel bir durum
    VIPTEN_ITEM_AL = 9  # VIP'ten item alma işlemi
    MERDIVEN_TIKLAMA = 10  # Merdiven tıklama durumu
    OYUNDANALTF4 = 11  # Oyundan çıkış durumu
    KUTU_BIR =10
    KUTU_IKI =11






class AnvilBotGUI:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Anvil Bot Control Panel")
        self.root.geometry("500x300")
        self.root.configure(bg='#2b2b2b')

        # Her zaman üstte tut
        self.root.attributes('-topmost', True)

        # Pencere kapatma olayını yakalayıp bot'u durdur
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # Bot instance
        self.bot = None
        self.bot_thread = None

        self.create_widgets()

    def create_widgets(self):
        # Title
        title_frame = tk.Frame(self.root, bg='#2b2b2b')
        title_frame.pack(fill='x', padx=10, pady=5)

        title_label = tk.Label(title_frame, text="🔨 ANVIL BOT CONTROL PANEL",
                               font=('Arial', 16, 'bold'), fg='#00ff00', bg='#2b2b2b')
        title_label.pack()

        # Control Frame
        control_frame = tk.Frame(self.root, bg='#2b2b2b')
        control_frame.pack(fill='x', padx=10, pady=5)

        # Buttons
        button_style = {'font': ('Arial', 10, 'bold'), 'width': 12, 'height': 2}

        self.start_btn = tk.Button(control_frame, text="▶ START", bg='#00aa00', fg='white',
                                   command=self.start_bot, **button_style)
        self.start_btn.pack(side='left', padx=5)

        self.start2_btn = tk.Button(control_frame, text="⏯ START 2", bg='#0066ff', fg='white',
                                    command=self.start_bot_state2, **button_style)
        self.start2_btn.pack(side='left', padx=5)

        self.pause_btn = tk.Button(control_frame, text="⏸ PAUSE", bg='#ffaa00', fg='white',
                                   command=self.pause_bot, **button_style)
        self.pause_btn.pack(side='left', padx=5)

        self.stop_btn = tk.Button(control_frame, text="⏹ STOP", bg='#ff4444', fg='white',
                                  command=self.stop_bot, **button_style)
        self.stop_btn.pack(side='left', padx=5)

        # Status Frame
        status_frame = tk.LabelFrame(self.root, text="Bot Durumu", fg='white', bg='#2b2b2b',
                                     font=('Arial', 10, 'bold'))
        status_frame.pack(fill='x', padx=10, pady=5)

        self.status_label = tk.Label(status_frame, text="Durum: DURDURULDU",
                                     fg='#ff4444', bg='#2b2b2b', font=('Arial', 12, 'bold'))
        self.status_label.pack(pady=5)

        self.state_label = tk.Label(status_frame, text="State: -",
                                    fg='#aaaaaa', bg='#2b2b2b', font=('Arial', 10))
        self.state_label.pack(pady=2)

        # Log Frame
        log_frame = tk.LabelFrame(self.root, text="Bot Logları", fg='white', bg='#2b2b2b',
                                  font=('Arial', 10, 'bold'))
        log_frame.pack(fill='both', expand=True, padx=10, pady=5)

        self.log_text = scrolledtext.ScrolledText(log_frame, bg='#1e1e1e', fg='#00ff00',
                                                  font=('Consolas', 9), wrap='word')
        self.log_text.pack(fill='both', expand=True, padx=5, pady=5)

        # Settings Frame
        settings_frame = tk.LabelFrame(self.root, text="Ayarlar", fg='white', bg='#2b2b2b',
                                       font=('Arial', 10, 'bold'))
        settings_frame.pack(fill='x', padx=10, pady=5)

        # Password setting
        pwd_frame = tk.Frame(settings_frame, bg='#2b2b2b')
        pwd_frame.pack(fill='x', padx=5, pady=5)

        tk.Label(pwd_frame, text="Şifre:", fg='white', bg='#2b2b2b', font=('Arial', 9)).pack(side='left')
        self.password_var = tk.StringVar(value="vaz3jSAU401778@1")
        self.password_entry = tk.Entry(pwd_frame, textvariable=self.password_var, show='*',
                                       bg='#3c3c3c', fg='white', font=('Arial', 9), width=30)
        self.password_entry.pack(side='left', padx=5)

        # Topmost toggle button
        self.topmost_var = tk.BooleanVar(value=True)
        self.topmost_btn = tk.Checkbutton(settings_frame, text="📍 Üstte Tut",
                                          variable=self.topmost_var, command=self.toggle_topmost,
                                          bg='#2b2b2b', fg='white', selectcolor='#2b2b2b',
                                          font=('Arial', 9))
        self.topmost_btn.pack(side='left', padx=5)

        # Clear log button
        self.clear_btn = tk.Button(settings_frame, text="🗑 Logları Temizle",
                                   command=self.clear_logs, bg='#666666', fg='white',
                                   font=('Arial', 9))
        self.clear_btn.pack(side='right', padx=5)

        # Initial log
        self.log_message("🚀 Anvil Bot GUI başlatıldı!")
        self.log_message("📝 START butonuna basarak bot'u başlatabilirsiniz.")
        self.log_message("⌨ CAPS LOCK tuşu ile bot'u acil durdurabilirsiniz.")

        # Update status timer
        self.update_status()

    def log_message(self, message):
        """Log mesajı ekle"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        formatted_message = f"[{timestamp}] {message}\n"

        self.log_text.insert(tk.END, formatted_message)
        self.log_text.see(tk.END)

    def clear_logs(self):
        """Logları temizle"""
        self.log_text.delete(1.0, tk.END)
        self.log_message("🧹 Loglar temizlendi.")

    def toggle_topmost(self):
        """Üstte tutma özelliğini aç/kapat"""
        topmost_state = self.topmost_var.get()
        self.root.attributes('-topmost', topmost_state)
        if topmost_state:
            self.log_message("📍 GUI üstte tutma açıldı.")
        else:
            self.log_message("📍 GUI üstte tutma kapatıldı.")

    def on_closing(self):
        """Pencere kapatılırken bot'u durdur"""
        if self.bot and self.bot.running:
            self.bot.running = False
            # Hotkey dinleyicisini durdur
            if hasattr(self.bot, 'stop_hotkey_listener'):
                self.bot.stop_hotkey_listener()
            time.sleep(0.5)  # Bot'un durmasını bekle
        self.root.destroy()

    def start_bot(self):
        """Bot'u başlat"""
        if self.bot is None:
            self.bot = AnvilBot(gui_callback=self.log_message)
            self.bot.password = self.password_var.get()
            self.bot_thread = threading.Thread(target=self.bot.run, daemon=True)
            self.bot_thread.start()
            self.log_message("✅ Bot başlatıldı!")
        elif self.bot.paused:
            self.bot.paused = False
            self.bot.state = State.CAPTURE
            self.log_message("▶ Bot devam ettiriliyor...")
        else:
            self.log_message("⚠ Bot zaten çalışıyor!")

    def start_bot_state2(self):
        """Bot'u state 2'den başlat"""
        if self.bot is None:
            self.bot = AnvilBot(gui_callback=self.log_message)
            self.bot.password = self.password_var.get()
            self.bot.state = State.CAPTURE
            self.bot_thread = threading.Thread(target=self.bot.run, daemon=True)
            self.bot_thread.start()
            self.log_message("✅ Bot CAPTURE state'den başlatıldı!")
        elif self.bot.running:
            self.bot.state = State.CAPTURE
            self.bot.paused = False
            self.log_message("⏯ Bot CAPTURE state'e geçirildi!")
        else:
            self.log_message("⚠ Bot çalışmıyor! Önce START'a basın.")

    def pause_bot(self):
        """Bot'u duraklat"""
        if self.bot and self.bot.running:
            if not self.bot.paused:
                self.bot.paused = True
                self.log_message("⏸ Bot duraklatıldı.")
            else:
                self.log_message("⚠ Bot zaten duraklatılmış!")
        else:
            self.log_message("⚠ Bot çalışmıyor!")

    def stop_bot(self):
        """Bot'u durdur"""
        if self.bot and self.bot.running:
            self.bot.running = False
            # Hotkey dinleyicisini durdur
            if hasattr(self.bot, 'stop_hotkey_listener'):
                self.bot.stop_hotkey_listener()
            self.log_message("⏹ Bot durduruldu.")
            self.bot = None
            self.bot_thread = None
        else:
            self.log_message("⚠ Bot zaten durdurulmuş!")

    def update_status(self):
        """Durumu güncelle"""
        if self.bot and self.bot.running:
            if self.bot.paused:
                self.status_label.config(text="Durum: DURAKLATILDI", fg='#ffaa00')
            else:
                self.status_label.config(text="Durum: ÇALIŞIYOR", fg='#00aa00')
            self.state_label.config(text=f"State: {self.bot.state.name}")
        else:
            self.status_label.config(text="Durum: DURDURULDU", fg='#ff4444')
            self.state_label.config(text="State: -")

        # Her 500ms'de bir güncelle
        self.root.after(500, self.update_status)

    def run(self):
        """GUI'yi başlat"""
        self.root.mainloop()


class AnvilBot:
    def __init__(self, gui_callback=None):
        self.state = State.BASLANGIC  # Başlangıç state
        self.frame = None
        self.last_torch_obj = None  # (label,(cx,cy,w,h))
        self.anvil_retry_count = 0
        self.next_allowed_time = 0  # cooldown zaman kontrolü
        self.fabrıc_sayı = None
        self.password = "vaz3jSAU401778@1"
        self.running = False  # Bot durumu kontrolü
        self.paused = False  # Bot pause durumu
        self.input_thread = None  # Input thread kontrolü
        self.gui_callback = gui_callback  # GUI log callback
        self.hotkey_listener = None  # Hotkey dinleyicisi

    def paste_text(self):
        password = self.password
        # Metni panoya kopyala
        pyperclip.copy(password)
        time.sleep(0.2)
        print(password)
        pyautogui.hotkey('ctrl', 'v')  # Şifre alanına yapıştır

    def on_caps_lock_pressed(self):
        """Caps Lock tuşuna basıldığında bot'u durdur"""
        if self.running:
            self.running = False
            self.print_status("🚨 CAPS LOCK ile bot durduruldu!")
            if self.gui_callback:
                # GUI varsa stop butonunu da güncelle
                pass

    def start_hotkey_listener(self):
        """Caps Lock hotkey dinleyicisi başlat"""
        if not pynput_keyboard:
            self.print_status("⚠ pynput yüklü değil, hotkey çalışmayacak.")
            return

        def on_key_press(key):
            try:
                # Caps Lock tuşunu kontrol et
                if hasattr(key, 'name') and key.name == 'caps_lock':
                    self.on_caps_lock_pressed()
                elif key == pynput_keyboard.Key.caps_lock:
                    self.on_caps_lock_pressed()
            except AttributeError:
                # Bazı tuşlar name attribute'una sahip olmayabilir
                pass

        try:
            self.hotkey_listener = pynput_keyboard.Listener(on_press=on_key_press)
            self.hotkey_listener.daemon = True
            self.hotkey_listener.start()
            self.print_status("⌨ Caps Lock hotkey aktif edildi.")
        except Exception as e:
            self.print_status(f"⚠ Hotkey başlatma hatası: {e}")

    def stop_hotkey_listener(self):
        """Hotkey dinleyicisini durdur"""
        if self.hotkey_listener:
            try:
                self.hotkey_listener.stop()
                self.hotkey_listener = None
                self.print_status("⌨ Caps Lock hotkey durduruldu.")
            except Exception as e:
                self.print_status(f"⚠ Hotkey durdurma hatası: {e}")

    def start_input_listener(self):
        """Keyboard input dinleyicisi başlatır"""

        def input_listener():
            while self.running:
                try:
                    command = input().strip().lower()
                    if command == "stop":
                        print("\n[SISTEM] Bot durduruluyor...")
                        self.running = False
                        break
                    elif command == "start":
                        if self.paused:
                            print("\n[SISTEM] Bot devam ediyor...")
                            self.paused = False
                            self.state = State.CAPTURE
                        else:
                            print("\n[SISTEM] Bot zaten çalışıyor...")
                    elif command.startswith("start "):
                        try:
                            state_num = int(command.split()[1])
                            if state_num == 2:
                                print("\n[SISTEM] Bot CAPTURE state'den başlatılıyor...")
                                self.state = State.CAPTURE
                                self.paused = False
                            else:
                                print(f"\n[SISTEM] Geçersiz state numarası: {state_num}")
                                print("Kullanılabilir: start 2 (CAPTURE state'den başlat)")
                        except (ValueError, IndexError):
                            print("\n[SISTEM] Geçersiz komut formatı. Örnek: start 2")
                    elif command == "pause":
                        if not self.paused:
                            print("\n[SISTEM] Bot duraklatılıyor...")
                            self.paused = True
                        else:
                            print("\n[SISTEM] Bot zaten duraklatılmış...")
                    elif command == "status":
                        if self.running:
                            if self.paused:
                                print(f"\n[SISTEM] Durum: DURAKLATILDI - Mevcut State: {self.state.name}")
                            else:
                                print(f"\n[SISTEM] Durum: ÇALIŞIYOR - Mevcut State: {self.state.name}")
                        else:
                            print("\n[SISTEM] Bot durduruldu.")
                    elif command == "help":
                        print("\n[KOMUTLAR]")
                        print("start       - Bot'u devam ettir (duraklatıldıysa)")
                        print("start 2     - Bot'u CAPTURE state'den başlat")
                        print("stop        - Bot'u durdur")
                        print("pause       - Bot'u duraklat")
                        print("status      - Bot durumunu göster")
                        print("help        - Bu yardım menüsünü göster")
                    else:
                        if command:
                            print(f"\n[SISTEM] Bilinmeyen komut: '{command}'. 'help' yazarak komutları görebilirsiniz.")
                except EOFError:
                    break
                except Exception as e:
                    print(f"\n[HATA] Input listener hatası: {e}")

        self.input_thread = threading.Thread(target=input_listener, daemon=True)
        self.input_thread.start()

    def wait_for_knight_online_process(self):
        """Knight Online procesini bekler"""
        max_wait = 50  # 1 dakika bekle
        wait_time = 0

        while wait_time < max_wait:
            for proc in psutil.process_iter(['name']):
                if proc.info['name'] and 'knightonline' in proc.info['name'].lower():
                    self.print_status(f"Knight Online process bulundu: {proc.info['name']}")
                    return True
            time.sleep(2)
            wait_time += 2
            self.print_status(f"Knight Online process bekleniyor... ({wait_time}/{max_wait})")

        return False

    def find_knight_online_window(self):
        """Knight Online penceresini bul"""
        possible_titles = [
            "Knight OnLine Client",
            "Knight Online",
            "KnightOnline",
            "Knight OnLine",
            "KnightOnLine Client"
        ]

        for title in possible_titles:
            windows = gw.getWindowsWithTitle(title)
            if windows:
                self.print_status(f"Knight Online penceresi bulundu: {title}")
                return windows[0]

        # Alternatif yöntem: tüm pencereleri kontrol et
        all_windows = gw.getAllWindows()
        for window in all_windows:
            if window.title and ('knight' in window.title.lower() or 'online' in window.title.lower()):
                self.print_status(f"Olasi Knight Online penceresi: {window.title}")
                return window

        return None

    def bring_knight_online_to_front(self):
        """Knight Online penceresini öne getir"""
        window = self.find_knight_online_window()
        if window:
            try:
                if window.isMinimized:
                    window.restore()
                window.activate()
                time.sleep(1)
                self.print_status("Knight Online penceresi öne getirildi.")
                return True
            except Exception as e:
                self.print_status(f"Pencere öne getirme hatası: {e}")

        self.print_status("Knight Online penceresi bulunamadı veya öne getirilemedi.")
        return False

    def print_status(self, message):
        """Durum mesajlarını formatted olarak yazdır"""
        timestamp = time.strftime("%H:%M:%S")
        formatted_msg = f"[{timestamp}] {message}"
        print(formatted_msg)

        # GUI callback varsa kullan
        if self.gui_callback:
            self.gui_callback(message)

    def run(self):
        if not self.gui_callback:  # Console mode
            print("\n" + "=" * 50)
            print("    ANVIL BOT BAŞLATILDI")
            print("=" * 50)
            print("Komutlar:")
            print("  start       - Bot'u devam ettir")
            print("  start 2     - CAPTURE state'den başlat")
            print("  stop        - Bot'u durdur")
            print("  pause       - Bot'u duraklat")
            print("  status      - Bot durumunu göster")
            print("  help        - Yardım menüsü")
            print("=" * 50 + "\n")

        self.running = True

        # Caps Lock hotkey'i her zaman başlat
        self.start_hotkey_listener()

        if not self.gui_callback:  # Sadece console mode'da input listener başlat
            self.start_input_listener()

        with mss.mss() as sct:
            while self.running:
                # Bot duraklatıldıysa bekle
                if self.paused:
                    time.sleep(0.1)
                    continue

                now = time.time()
                if now < self.next_allowed_time:
                    time.sleep(0.05)
                    continue
                # Bot durduruldu kontrolü
                if not self.running:
                    break

                # BAŞLANGIÇ DURUMU
                if self.state == State.BASLANGIC:
                    self.print_status("Başlangıç durumu - Knight Online başlatılıyor...")

                    # Knight Online Launcher'ı aç
                    self.print_status("Knight Online Launcher açılıyor...")
                    subprocess.Popen(r"C:\NTTGame\KnightOnlineEn\Launcher.exe")
                    time.sleep(1.5)  # Launcher'in açılmasını bekle

                    # Başlat butonuna tıkla
                    self.print_status("Başlat butonuna tıklanıyor...")
                    mous.move_and_click("left", 0.3, 979, 730)
                    time.sleep(0.5)

                    # Knight Online process'ini bekle
                    self.print_status("Knight Online process'i bekleniyor...")
                    if not self.wait_for_knight_online_process():
                        self.print_status("⚠ Knight Online process bulunamadı! Tekrar deneniyor...")
                        time.sleep(5)
                        continue

                    # Pencereyi bul ve öne getir
                    self.print_status("Knight Online penceresi aranıyor...")
                    max_attempts = 120  # 2 dakika bekle
                    attempts = 0

                    while attempts < max_attempts:
                        if self.bring_knight_online_to_front():
                            break
                        time.sleep(2)
                        attempts += 1
                        self.print_status(f"Pencere bekleniyor... ({attempts}/{max_attempts})")

                    if attempts >= max_attempts:
                        self.print_status("⚠ Knight Online penceresi bulunamadı! Tekrar deneniyor...")
                        time.sleep(5)
                        continue

                    # Oyun tamamen yüklenene kadar bekle
                    self.print_status("Oyun yüklenmesi bekleniyor...")
                    time.sleep(1)  # Oyunun yüklenmesi için yeterli süre

                    # Pencereyi tekrar öne getir
                    self.bring_knight_online_to_front()
                    time.sleep(1)

                    # Animasyonu hızlandır
                    self.print_status("Animasyon hızlandırılıyor...")
                    mous.move_and_click("left", 0.3, 1560, 535)
                    time.sleep(0.5)
                    mous.move_and_click("left", 0.3, 1560, 535)
                    time.sleep(0.5)
                    key.press_key(0x1C, 0.3)  # Enter
                    time.sleep(1)

                    # Login ekranı için bekle
                    self.print_status("Login ekranı bekleniyor...")
                    time.sleep(1)

                    # ID ve Şifre girme
                    self.print_status("Kullanıcı bilgileri giriliyor...")
                    mous.move_and_click("left", 0.3, 964, 451)  # ID alanı
                    time.sleep(0.5)
                    pyautogui.write("akoseoglu", interval=0.1)
                    time.sleep(0.2)

                    mous.move_and_click("left", 0.3, 964, 505)  # Şifre alanı
                    time.sleep(0.2)
                    self.paste_text()
                    time.sleep(0.2)

                    # Login
                    key.press_key(0x1C, 0.3)  # Enter
                    time.sleep(1)
                    key.press_key(0x1C, 0.3)  # Enter tekrar
                    time.sleep(1)

                    # Server seçimi
                    self.print_status("Server seçiliyor...")
                    mous.move_and_click("left", 0.3, 908, 356)  # Destan
                    time.sleep(0.2)
                    mous.move_and_click("left", 0.3, 1117, 404)  # Destan3
                    time.sleep(0.2)
                    key.press_key(0x1C, 0.3)  # Enter
                    time.sleep(2)

                    # Karakter seçimi ve giriş
                    key.press_key(0x1C, 0.3)  # Enter
                    time.sleep(1)
                    key.press_key(0x1C, 0.3)  # Enter
                    time.sleep(1)
                    key.press_key(0x1C, 0.3)  # Enter
                    time.sleep(10)  # Oyun içi loading

                    # Town'a git ve hazırlık
                    self.print_status("Town'a gidiliyor...")
                    mous.move_and_click("left", 0.3, 1665, 1052)  # Town
                    time.sleep(0.025)
                    # Kamerayı ayarla
                    pyautogui.scroll(-4500)
                    time.sleep(0.1)
                    time.sleep(1)
                    key.press_key(0x18, 0.3)  # O tuşu (oyun başlat)
                    time.sleep(0.1)
                    key.press_key(0x01, 0.3)  # Esc (menü kapat)
                    time.sleep(0.1)





                    # CAPTURE state'e geç
                    self.state = State.CAPTURE
                    self.print_status("✅ Knight Online giriş başarılı! CAPTURE durumuna geçildi.")
                    continue

                if self.state == State.CAPTURE:
                    self.frame = grab_screen(sct)
                    self.state = State.DETECT_TORCHES

                elif self.state == State.DETECT_TORCHES:
                    detections = detect_objects(self.frame)
                    torch_pick = pick_closest_to_center(detections, TORCH_CLASSES)
                    if torch_pick is None:
                        # Torch yok; bir süre bekle, yeniden capture
                        time.sleep(LOOP_POLL)
                        self.state = State.CAPTURE
                        continue
                    self.last_torch_obj = torch_pick  # sakla
                    self.state = State.CLICK_TORCH

                elif self.state == State.CLICK_TORCH:
                    if not self.last_torch_obj:
                        self.state = State.CAPTURE
                        continue
                    label, (cx, cy, w, h) = self.last_torch_obj
                    scr_cx, _ = screen_center()
                    click_x, click_y = inward_and_down(cx, cy, w, h, scr_cx, TORCH_INNER_RATIO)

                    self.print_status(f"[{label}] meşalesine tıklanıyor: ({click_x},{click_y})")
                    mous.move_and_click("left", 0.5, click_x, click_y)
                    self.anvil_retry_count = 0
                    key.press_key(0x2D, 0.3)
                    time.sleep(0.01)
                    time.sleep(8)
                    self.state = State.DETECT_ANVIL
                    # küçük gecikme ki UI güncellensin


                elif self.state == State.DETECT_ANVIL:
                    # Her denemede yeni ekran al
                    self.frame = grab_screen(sct)
                    detections = detect_objects(self.frame)
                    anvil_pick = pick_closest_to_center(detections, {ANVIL_CLASS})
                    if anvil_pick is not None:
                        self.last_anvil_obj = anvil_pick  # (label,(cx,cy,w,h))
                        self.state = State.CLICK_ANVIL
                        continue

                    # bulunamadı
                    self.anvil_retry_count += 1
                    if self.anvil_retry_count >= ANVIL_MAX_RETRIES:
                        self.state = State.FALLBACK_ANVIL_COORD
                    else:
                        self.print_status(
                            f"AM bulunamadı (deneme {self.anvil_retry_count}/{ANVIL_MAX_RETRIES}). Tekrar aranacak.")
                        time.sleep(ANVIL_RETRY_DELAY)

                elif self.state == State.CLICK_ANVIL:

                    ustu_doldu = False

                    label, (cx, cy, w, h) = self.last_anvil_obj
                    click_x, click_y = inward_and_down(cx, cy, w, h, scr_cx, ANVIL_INNER_RATIO)
                    self.print_status(f"[{label}] hedefe tıklanıyor: ({click_x},{click_y})")
                    mous.move_and_click("left", 2, click_x, click_y + 20)
                    self.print_status(f"AM tıklandı, {POST_CLICK_ANVIL_WAIT:.0f} sn bekleniyor.")
                    time.sleep(POST_CLICK_ANVIL_WAIT)
                    self.state = State.F9BASM


                elif self.state == State.MERDIVEN_TIKLAMA:
                    label, (cx, cy, w, h) = self.last_anvil_obj
                    click_x, click_y = inward_and_down(cx, cy, w, h, scr_cx, MERDIVEN_RATIO)
                    self.print_status(f"[{label}] hedefe tıklanıyor: ({click_x},{click_y})")
                    mous.move_and_click("left", 1.2, click_x, click_y + 95)
                    self.print_status(f"AM tıklandı, {POST_CLICK_ANVIL_WAIT:.0f} sn bekleniyor.")
                    time.sleep(POST_CLICK_ANVIL_WAIT)
                    self.state = State.COOLDOWN

                elif self.state == State.FALLBACK_ANVIL_COORD:
                    self.print_status("AM bulunamadı, ön tanımlı koordinata tıklanıyor.")
                    time.sleep(3)
                    mous.move_and_click("left", 0.3, PREDEFINED_X, PREDEFINED_Y)
                    time.sleep(2)
                    key.press_key(0x1F, 0.3)
                    time.sleep(0.2)
                    self.state = State.COOLDOWN




                elif self.state == State.F9BASM:
                    time.sleep(2)
                    key.press_key(0x43, 0.1)
                    time.sleep(0.1)
                    key.press_key(0x43, 0.1)
                    time.sleep(0.1)
                    key.press_key(0x30, 0.1)
                    time.sleep(0.1)
                    key.press_key(0x11, 0.1)#w basıp anvile yaklaşma
                    time.sleep(1)
                    key.press_key(0x17, 0.1)  # Anvile geldi inventory açmak için ı bastı
                    time.sleep(0.1)
                    mous.move_and_click("left", 0.1, 1573, 333)  # vip key tıklama
                    time.sleep(0.01)
                    rows = [79, 129, 179, 229, 279, 329, 379, 429]  # Y koordinatları
                    columns = [1210, 1260, 1310, 1360, 1410, 1460]  # X koordinatları

                    for y in rows:
                        for x in columns:
                            mous.move_and_click("right", 0.30, x, y)  # VIP key içerisindeki itemlere sağ tık
                            time.sleep(0.01)
                    time.sleep(0.05)
                    key.press_key(0x30, 0.1)
                    time.sleep(0.1)

                    def perform_upgrade(item_coords):
                        """
                        Upgrade aşamalarını gerçekleştiren fonksiyon.
                        :param item_coords: Basilacak itemin kordinatları (x, y).
                        """
                        # Anvil'e sağ tıkla
                        mous.move_and_click("right", ITEM_BASMA_SURE, 962, 330)
                        time.sleep(0.01)

                        # Confirm
                        mous.move_and_click("left", ITEM_BASMA_SURE, 963, 516)
                        time.sleep(0.01)

                        # Upgrade scroll sağ tıkla
                        mous.move_and_click("right", ITEM_BASMA_SURE, 1555, 425)
                        time.sleep(0.01)

                        # Basilacak İtem'in koordinatına sağ tıkla
                        mous.move_and_click("right", ITEM_BASMA_SURE, item_coords[0], item_coords[1])
                        time.sleep(0.01)

                        # İtemi basmak için onay
                        mous.move_and_click("left", ITEM_BASMA_SURE, 1635, 327)
                        time.sleep(0.01)

                        # İtemin basılması için son onay
                        mous.move_and_click("left", ITEM_BASMA_SURE, 1627, 448)
                        time.sleep(0.01)

                    # Farklı item koordinatlarını içeren liste
                    item_coordinates = [
                        (1611, 423), (1660, 425), (1710, 425), (1760, 425), (1810, 425), (1860, 425),
                        (1560, 475), (1610, 475), (1660, 475), (1710, 475), (1760, 475), (1810, 475), (1860, 475),
                        (1560, 525), (1610, 525), (1660, 525), (1710, 525), (1760, 525), (1810, 525), (1860, 525),
                        (1560, 575), (1610, 575), (1660, 575), (1710, 575), (1760, 575), (1810, 575), (1860, 575),
                        (1611, 423), (1660, 425), (1710, 425), (1760, 425)
                    ]

                    # Döngü ile upgrade işlemini tekrar et
                    for coords in item_coordinates:
                        perform_upgrade(coords)

                    key.press_key(0x43, 0.1)
                    time.sleep(0.1)
                    key.press_key(0x43, 0.1)
                    time.sleep(2.5)


                    self.state = State.OYUNDANALTF4

                elif self.state== State.OYUNDANALTF4:
                    subprocess.Popen("taskkill /F /IM KnightOnLine.exe", shell=True)  # Knight Online'ı kapat
                    self.print_status("İşlem tamamlandı, Knight Online kapatılıyor. Yeniden başlatılacak...")
                    time.sleep(1.5)
                    self.state=State.BASLANGIC





                elif self.state == State.COOLDOWN:
                    # Döngüyü biraz rahatlat
                    self.next_allowed_time = time.time() + COOLDOWN_AFTER_SEQUENCE
                    self.state = State.CAPTURE

                else:
                    self.state = State.CAPTURE

                time.sleep(LOOP_POLL)

        # Hotkey dinleyicisini durdur
        self.stop_hotkey_listener()

        if not self.gui_callback:
            print("\n[SISTEM] Bot durduruldu. Çıkış yapılıyor...")
        else:
            self.print_status("🛑 Bot durduruldu.")


# ----------------- Ana Çalıştırıcı -----------------
if __name__ == "__main__":
    import sys

    # GUI mode mi console mode mi kontrol et
    if len(sys.argv) > 1 and sys.argv[1] == "--console":
        # Console mode
        bot = AnvilBot()
        bot.run()
    else:
        # GUI mode (default)
        gui = AnvilBotGUI()
        gui.run()

