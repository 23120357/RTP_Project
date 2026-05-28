from tkinter import *
import tkinter.messagebox
from PIL import Image, ImageTk
import socket, threading, time, queue, io

from RtpPacket import RtpPacket

CACHE_FILE_NAME = "cache-"
CACHE_FILE_EXT  = ".jpg"

MAX_UDP_PAYLOAD    = 1450
RTP_HEADER_SIZE    = 12
MAX_RTP_PAYLOAD    = MAX_UDP_PAYLOAD - RTP_HEADER_SIZE
REASSEMBLY_TIMEOUT = 1.0 

VIDEO_SIZE = (640, 360) 

class Client:
	INIT    = 0
	READY   = 1
	PLAYING = 2
	state   = INIT
	
	SETUP    = 0
	PLAY     = 1
	PAUSE    = 2
	TEARDOWN = 3
	SWITCH   = 4
	
	MIN_BUFFER_SIZE = 10
	
	def __init__(self, master, serveraddr, serverport, rtpport):
		self.master   = master
		self.master.protocol("WM_DELETE_WINDOW", self.handler)
		self.serverAddr    = serveraddr
		self.serverPort    = int(serverport)
		self.rtpPort       = int(rtpport)
		
		self.quality = StringVar(value="SD")
		self.fileName = "movie/movie_SD.Mjpeg"
		self.transport = "UDP"

		self.createWidgets()
		self.rtspSeq       = 0
		self.sessionId     = 0
		self.requestSent   = -1
		self.teardownAcked = 0
		self.connectToServer()
		self.frameNbr = 0

		self.frameCache = queue.Queue(maxsize=20)
		self.is_buffering = True

		self.reassemblyBuffer = {}
		self.bufferLock       = threading.Lock()
		
	def createWidgets(self):
		self.master.config(padx=10, pady=10)
		
		self.timerLabel = Label(self.master, text="00:00", font=("Helvetica", 11))
		self.timerLabel.grid(row=1, column=0, columnspan=4, padx=5, pady=[15, 0])

		self.setup = Button(self.master, width=15, padx=3, pady=3, text="Setup", command=self.setupMovie)
		self.setup.grid(row=3, column=0, padx=2, pady=2)
		
		self.start = Button(self.master, width=15, padx=3, pady=3, text="Play", command=self.playMovie)
		self.start.grid(row=3, column=1, padx=2, pady=2)
		
		self.pause = Button(self.master, width=15, padx=3, pady=3, text="Pause", command=self.pauseMovie)
		self.pause.grid(row=3, column=2, padx=2, pady=2)
		
		self.teardown = Button(self.master, width=15, padx=3, pady=3, text="Teardown", command=self.exitClient)
		self.teardown.grid(row=3, column=3, padx=2, pady=2)

		qualityFrame = Frame(self.master)
		qualityFrame.grid(row=2, column=0, columnspan=4, pady=[0, 5])
		
		self.radioSd = Radiobutton(qualityFrame, text="SD (960x540)", variable=self.quality, value="SD", command=self.onQualityChange)
		self.radioHd = Radiobutton(qualityFrame, text="HD (1280x720)", variable=self.quality, value="HD", command=self.onQualityChange)
		self.radioFhd = Radiobutton(qualityFrame, text="FHD (1920x1080)", variable=self.quality, value="FHD", command=self.onQualityChange)
		
		self.radioSd.pack(side=LEFT, padx=15)
		self.radioHd.pack(side=LEFT, padx=15)
		self.radioFhd.pack(side=LEFT, padx=15)
		
		self.dummy_img = ImageTk.PhotoImage(Image.new("RGB", VIDEO_SIZE, "black"))
		self.label = Label(self.master, image=self.dummy_img)
		self.label.grid(row=0, column=0, columnspan=4, padx=5, pady=5)
		
		self.updateUI()

	def onQualityChange(self):
		val = self.quality.get()
		if val == "SD":
			self.fileName = "movie/movie_SD.Mjpeg"
			self.transport = "UDP"
		elif val == "HD":
			self.fileName = "movie/movie_HD.Mjpeg"
			self.transport = "TCP"
		elif val == "FHD":
			self.fileName = "movie/movie_FHD.Mjpeg"
			self.transport = "TCP"
		
		with self.frameCache.mutex:
			self.frameCache.queue.clear()

		self.is_buffering = True
			
		self.timerLabel.configure(text="Buffering...")
		
		if self.state in [self.READY, self.PLAYING]:
			self.sendRtspRequest(self.SWITCH)

	def updateUI(self):
		if self.state == self.INIT:
			self.setup['state'] = NORMAL
			self.start['state'] = DISABLED
			self.pause['state'] = DISABLED
			self.teardown['state'] = NORMAL
		elif self.state == self.READY:
			self.setup['state'] = DISABLED
			self.start['state'] = NORMAL
			self.pause['state'] = DISABLED
			self.teardown['state'] = NORMAL
		elif self.state == self.PLAYING:
			self.setup['state'] = DISABLED
			self.start['state'] = DISABLED
			self.pause['state'] = NORMAL
			self.teardown['state'] = NORMAL

	def setupMovie(self):
		if self.state == self.INIT:
			self.sendRtspRequest(self.SETUP)
	
	def exitClient(self):
			self.sendRtspRequest(self.TEARDOWN)		
			self.master.destroy()

	def pauseMovie(self):
		if self.state == self.PLAYING:
			self.sendRtspRequest(self.PAUSE)
	
	def playMovie(self):
		if self.state == self.READY and self.requestSent != self.PLAY:
			if self.frameCache.qsize() < self.MIN_BUFFER_SIZE:
				self.is_buffering = True

			self.sendRtspRequest(self.PLAY)
			self.master.after(50, self.displayFrame)

	def listenRtpUDP(self):
		while True:
			try:
				data = self.rtpUdpSocket.recv(MAX_UDP_PAYLOAD + 50)
				if not data: continue

				rtpPacket = RtpPacket()
				rtpPacket.decode(data)

				ts, seq, is_last, payload = rtpPacket.timestamp(), rtpPacket.seqNum(), (rtpPacket.marker() == 1), rtpPacket.getPayload()
				# 1. Log in ra từng mảnh (Fragment) nhận được
				print(f"  -> [UDP RX FRAG] ts={ts} | Seq={seq:5d} | Marker={rtpPacket.marker()} | Len={len(payload):4d} bytes")

				with self.bufferLock:
					if ts not in self.reassemblyBuffer:
						self.reassemblyBuffer[ts] = {'fragments': {}, 'min_seq': seq, 'max_seq': seq, 'marked': False, 'arrived': time.time()}
					entry = self.reassemblyBuffer[ts]
					entry['fragments'][seq] = payload
					if is_last: entry['marked'] = True

					if entry['marked']:
						seqs = list(entry['fragments'].keys())
						if max(seqs) - min(seqs) > 32768: seqs.sort(key=lambda x: x if x > 32768 else x + 65536)
						else: seqs.sort()

						if len(entry['fragments']) == (seqs[-1] - seqs[0] + 1):
							assembled = b''.join(entry['fragments'][s % 65536] for s in seqs)

							# 2. Log báo hiệu gộp thành công 1 Frame
							print(f"[UDP RX COMPLETE] ts={ts} | Assembled {len(seqs)} frags | Total={len(assembled)} bytes")

							# BỎ CHẶN KIỂM TRA ĐẦY. Hàm put() sẽ Block luồng này lại nếu đầy
							self.frameCache.put(assembled)
							del self.reassemblyBuffer[ts]
			except Exception:
				if self.teardownAcked == 1: break

	def acceptTcp(self):
		while True:
			try:
				conn, addr = self.rtpTcpListener.accept()
				if hasattr(self, 'rtpTcpSocket'):
					try: self.rtpTcpSocket.close()
					except: pass
					
				self.rtpTcpSocket = conn
				threading.Thread(target=self.listenRtpTCP, args=(conn,), daemon=True).start()
			except: break

	def listenRtpTCP(self, conn):
		def recvall(sock, n):
			data = bytearray()
			while len(data) < n:
				packet = sock.recv(n - len(data))
				if not packet: return None
				data.extend(packet)
			return data

		while True:
			try:
				length_bytes = recvall(conn, 4)
				if not length_bytes: break
				msg_len = int.from_bytes(length_bytes, byteorder='big')
				
				packet_data = recvall(conn, msg_len)
				if not packet_data: break
				
				rtpPacket = RtpPacket()
				rtpPacket.decode(packet_data)
				
				# Thêm log để thấy TCP nhận Frame nguyên vẹn
				print(f"[TCP RX COMPLETE] ts={rtpPacket.timestamp()} | Seq={rtpPacket.seqNum():5d} | Total={len(rtpPacket.getPayload()):6d} bytes")

				# Hàm put() sẽ tự động Block nếu cache đã đầy.
				# Điều này tạo Áp lực ngược (Backpressure) làm ngừng hàm recvall()
				self.frameCache.put(rtpPacket.getPayload())
			except Exception: break

	def displayFrame(self):
		start_time = time.time()  # Bấm giờ xem CPU xử lý mất bao lâu
		try:
			if self.is_buffering:
				if self.frameCache.qsize() < self.MIN_BUFFER_SIZE:
					self.timerLabel.configure(text=f"Buffering... ({self.frameCache.qsize()}/{self.MIN_BUFFER_SIZE})")
					return
				else:
					self.is_buffering = False

			if not self.frameCache.empty():
				frame_data = self.frameCache.get_nowait()
				
				# TỐI ƯU 1: Giải mã ảnh thẳng từ RAM, không ghi ra ổ cứng nữa!
				image_stream = io.BytesIO(frame_data)
				img = Image.open(image_stream)
				
				# TỐI ƯU 2: Dùng thuật toán BILINEAR siêu nhẹ thay cho LANCZOS
				resample_mode = getattr(Image, 'Resampling', Image).BILINEAR
				img = img.resize(VIDEO_SIZE, resample_mode)
				photo = ImageTk.PhotoImage(img)
				
				self.label.configure(image=photo)
				self.label.image = photo
				
				self.frameNbr += 1
				mins, secs = divmod(self.frameNbr // 20, 60)
				self.timerLabel.configure(text=f"{mins:02d}:{secs:02d}")
			else:
				if self.state == self.PLAYING:
					self.timerLabel.configure(text="Buffering...")
					self.is_buffering = True
		except Exception:
			pass 
		finally:
			if self.state == self.PLAYING or self.requestSent == self.PLAY:
				# TỐI ƯU 3: Bù trừ thời gian chạy CPU để duy trì đúng nhịp 20 FPS (50ms)
				process_time = int((time.time() - start_time) * 1000)
				wait_time = max(1, 50 - process_time)  # Nếu CPU mất 20ms, chỉ bắt Tkinter đợi 30ms thôi
				self.master.after(wait_time, self.displayFrame)

	def watchdogThread(self):
		while True:
			time.sleep(0.2)
			if self.teardownAcked == 1: break
			now = time.time()
			with self.bufferLock:
				stale = [ts for ts, info in self.reassemblyBuffer.items() if now - info['arrived'] > REASSEMBLY_TIMEOUT]
				for ts in stale:
					del self.reassemblyBuffer[ts]
	
	def updateMovie(self, imageFile):
		try:
			resample_mode = getattr(Image, 'Resampling', Image).LANCZOS
			img = Image.open(imageFile)
			img = img.resize(VIDEO_SIZE, resample_mode)
			photo = ImageTk.PhotoImage(img)
			self.label.configure(image=photo)
			self.label.image = photo
		except Exception: pass

	def connectToServer(self):
		self.rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		try: self.rtspSocket.connect((self.serverAddr, self.serverPort))
		except: tkinter.messagebox.showwarning('Connection Failed', 'Connection to \'%s\' failed.' % self.serverAddr)
	
	def sendRtspRequest(self, requestCode):
		if requestCode == self.SETUP and self.state == self.INIT:
			threading.Thread(target=self.recvRtspReply, daemon=True).start()
			self.rtspSeq = 1
			request  = f"SETUP {self.fileName} RTSP/1.0\nCSeq: {self.rtspSeq}\n"
			transport_str = "RTP/UDP" if self.transport == "UDP" else "RTP/TCP"
			request += f"Transport: {transport_str}; client_port= {self.rtpPort}"
			self.requestSent = self.SETUP
		elif requestCode == self.PLAY and self.state == self.READY:
			self.rtspSeq += 1
			request  = f"PLAY {self.fileName} RTSP/1.0\nCSeq: {self.rtspSeq}\nSession: {self.sessionId}"
			self.requestSent = self.PLAY
		elif requestCode == self.PAUSE and self.state == self.PLAYING:
			self.rtspSeq += 1
			request  = f"PAUSE {self.fileName} RTSP/1.0\nCSeq: {self.rtspSeq}\nSession: {self.sessionId}"
			self.requestSent = self.PAUSE
		elif requestCode == self.TEARDOWN and not self.state == self.INIT:
			self.rtspSeq += 1
			request  = f"TEARDOWN {self.fileName} RTSP/1.0\nCSeq: {self.rtspSeq}\nSession: {self.sessionId}"
			self.requestSent = self.TEARDOWN
		elif requestCode == self.SWITCH and self.state in [self.READY, self.PLAYING]:
			self.rtspSeq += 1
			request  = f"SWITCH {self.fileName} RTSP/1.0\nCSeq: {self.rtspSeq}\nSession: {self.sessionId}\n"
			transport_str = "RTP/UDP" if self.transport == "UDP" else "RTP/TCP"
			request += f"Transport: {transport_str}\nClientFrame: {self.frameNbr}"
			self.requestSent = self.SWITCH
		else: return

		print('\n' + '\n'.join([f"C: {line}" for line in request.split('\n') if line.strip()]))
		self.rtspSocket.send(request.encode())
	
	def recvRtspReply(self):
		while True:
			reply = self.rtspSocket.recv(1024)
			if reply: self.parseRtspReply(reply.decode("utf-8"))
			if self.requestSent == self.TEARDOWN:
				self.rtspSocket.shutdown(socket.SHUT_RDWR)
				self.rtspSocket.close()
				break
	
	def parseRtspReply(self, data):
		lines  = data.split('\n')
		seqNum = int(lines[1].split(' ')[1])
		if seqNum == self.rtspSeq:
			session = int(lines[2].split(' ')[1])
			if self.sessionId == 0: self.sessionId = session
			if self.sessionId == session and int(lines[0].split(' ')[1]) == 200:
				if self.requestSent == self.SETUP:
					self.state = self.READY
					self.openRtpPort()
				elif self.requestSent == self.PLAY: self.state = self.PLAYING
				elif self.requestSent == self.PAUSE: self.state = self.READY
				elif self.requestSent == self.TEARDOWN:
					self.state = self.INIT
					self.teardownAcked = 1
				self.master.after(0, self.updateUI)
	
	def openRtpPort(self):
			if not hasattr(self, 'rtpUdpSocket'):
				self.rtpUdpSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
				self.rtpUdpSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
				self.rtpUdpSocket.settimeout(0.5)
				try:
					self.rtpUdpSocket.bind(('', self.rtpPort))
					threading.Thread(target=self.listenRtpUDP, daemon=True).start()
					threading.Thread(target=self.watchdogThread, daemon=True).start()
				except Exception: pass

			if not hasattr(self, 'rtpTcpListener'):
				self.rtpTcpListener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
				self.rtpTcpListener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
				try:
					self.rtpTcpListener.bind(('', self.rtpPort))
					self.rtpTcpListener.listen(5)
					threading.Thread(target=self.acceptTcp, daemon=True).start()
				except Exception: pass

	def handler(self):
		self.pauseMovie()
		if tkinter.messagebox.askokcancel("Quit?", "Are you sure you want to quit?"): self.exitClient()
		else: self.playMovie()