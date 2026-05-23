from tkinter import *
import tkinter.messagebox
from PIL import Image, ImageTk
import socket, threading, sys, traceback, os, time, queue

from RtpPacket import RtpPacket

CACHE_FILE_NAME = "cache-"
CACHE_FILE_EXT  = ".jpg"

# ── MTU / Fragmentation constants (must match ServerWorker.py) ─────────────────
MAX_UDP_PAYLOAD    = 1400   # bytes
RTP_HEADER_SIZE    = 12     # bytes
MAX_RTP_PAYLOAD    = MAX_UDP_PAYLOAD - RTP_HEADER_SIZE  # 1388 bytes per fragment
REASSEMBLY_TIMEOUT = 1.0    # seconds — drop incomplete frames after this interval
# ────────────────────────────────────────────────────────────────────────────────

class Client:
	INIT    = 0
	READY   = 1
	PLAYING = 2
	state   = INIT
	
	SETUP    = 0
	PLAY     = 1
	PAUSE    = 2
	TEARDOWN = 3
	
	def __init__(self, master, serveraddr, serverport, rtpport, filename):
		self.master   = master
		self.master.protocol("WM_DELETE_WINDOW", self.handler)
		self.createWidgets()
		self.serverAddr    = serveraddr
		self.serverPort    = int(serverport)
		self.rtpPort       = int(rtpport)
		self.fileName      = filename
		self.rtspSeq       = 0
		self.sessionId     = 0
		self.requestSent   = -1
		self.teardownAcked = 0
		self.connectToServer()
		self.frameNbr = 0

		# ── Thread-safe frame queue ──────────────────────────────────────────────
		# listenRtp (background thread) pushes assembled frames here.
		# displayFrame (main/Tk thread) pops and renders them via after().
		self.frameQueue = queue.Queue()

		# ── Timestamp-keyed reassembly buffer ────────────────────────────────────
		# Key   : RTP timestamp (same for every fragment of one video frame)
		# Value : {
		#   'fragments' : { seq_num (int) : payload (bytes) },
		#   'min_seq'   : int,   # lowest seq num seen for this ts
		#   'max_seq'   : int,   # highest seq num seen for this ts
		#   'marked'    : bool,  # True once the M=1 (last-fragment) packet arrives
		#   'arrived'   : float  # wall-clock time of first fragment (for watchdog)
		# }
		self.reassemblyBuffer = {}
		self.bufferLock       = threading.Lock()   # guards reassemblyBuffer
		# ────────────────────────────────────────────────────────────────────────
		
	def createWidgets(self):
		"""Build GUI."""
		self.timerLabel = Label(self.master, text="00:00", font=("Helvetica", 12))
		self.timerLabel.grid(row=1, column=0, columnspan=4, padx=5, pady=5)

		self.setup = Button(self.master, width=20, padx=3, pady=3)
		self.setup["text"]    = "Setup"
		self.setup["command"] = self.setupMovie
		self.setup.grid(row=2, column=0, padx=2, pady=2)
		
		self.start = Button(self.master, width=20, padx=3, pady=3)
		self.start["text"]    = "Play"
		self.start["command"] = self.playMovie
		self.start.grid(row=2, column=1, padx=2, pady=2)
		
		self.pause = Button(self.master, width=20, padx=3, pady=3)
		self.pause["text"]    = "Pause"
		self.pause["command"] = self.pauseMovie
		self.pause.grid(row=2, column=2, padx=2, pady=2)
		
		self.teardown = Button(self.master, width=20, padx=3, pady=3)
		self.teardown["text"]    = "Teardown"
		self.teardown["command"] = self.exitClient
		self.teardown.grid(row=2, column=3, padx=2, pady=2)
		
		self.label = Label(self.master, height=19)
		self.label.grid(row=0, column=0, columnspan=4, sticky=W+E+N+S, padx=5, pady=5)
		
		self.updateUI()

	def updateUI(self):
		"""Update GUI buttons state based on current client state."""
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
		"""Setup button handler."""
		if self.state == self.INIT:
			self.sendRtspRequest(self.SETUP)
	
	def exitClient(self):
		"""Teardown button handler."""
		self.sendRtspRequest(self.TEARDOWN)		
		self.master.destroy()
		try:
			os.remove(CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT)
		except:
			pass

	def pauseMovie(self):
		"""Pause button handler."""
		if self.state == self.PLAYING:
			self.sendRtspRequest(self.PAUSE)
	
	def playMovie(self):
		"""Play button handler.
		
		Starts two background daemon threads:
		  • listenRtp      — receives UDP fragments and reassembles frames
		  • watchdogThread — drops stale incomplete frames after REASSEMBLY_TIMEOUT
		
		Kicks off the Tkinter-safe display loop via after().
		"""
		if self.state == self.READY:
			self.playEvent = threading.Event()
			self.playEvent.clear()

			threading.Thread(target=self.listenRtp,      daemon=True).start()
			threading.Thread(target=self.watchdogThread, daemon=True).start()

			self.sendRtspRequest(self.PLAY)

			# Start the main-thread display polling loop
			self.master.after(50, self.displayFrame)

	def listenRtp(self):
		"""Background thread: receive RTP fragments and reassemble into frames.

		Reassembly strategy (standard RTP fields only, no custom header):
		  • Fragments are grouped by their shared RTP *timestamp*.
		  • A frame is declared complete when BOTH conditions hold:
		      1. A packet with Marker bit == 1 has arrived (last fragment).
		      2. No sequence-number gaps: len(fragments) == max_seq − min_seq + 1
		  • Fragments are sorted by sequence number before joining.
		  • Completed frames are pushed into self.frameQueue for GUI rendering.
		"""
		while True:
			try:
				data = self.rtpSocket.recv(MAX_UDP_PAYLOAD + 50)
				if not data:
					continue

				rtpPacket = RtpPacket()
				rtpPacket.decode(data)

				ts      = rtpPacket.timestamp()
				seq     = rtpPacket.seqNum()
				is_last = (rtpPacket.marker() == 1)
				payload = rtpPacket.getPayload()

				print(f"  -> [FRAG IN] ts={ts} | seq={seq:5d} | marker={rtpPacket.marker()} | len={len(payload):4d} bytes")

				with self.bufferLock:
					# Initialise entry on first fragment seen for this timestamp
					if ts not in self.reassemblyBuffer:
						self.reassemblyBuffer[ts] = {
							'fragments': {},
							'min_seq':   seq,
							'max_seq':   seq,
							'marked':    False,
							'arrived':   time.time(),
						}

					entry = self.reassemblyBuffer[ts]
					entry['fragments'][seq] = payload

					if is_last:
						entry['marked'] = True

					# ── Completeness check ────────────────────────────────────
					if entry['marked']:
						# Lấy danh sách các sequence number hiện có
						seqs = list(entry['fragments'].keys())
						
						# Sắp xếp và xử lý lỗi quay vòng (wrap-around) của số 16-bit
						# Ví dụ: [0, 1, 65535] -> Nếu khoảng cách lớn hơn 32768, số nhỏ bị đẩy lên sau
						if max(seqs) - min(seqs) > 32768:
							seqs.sort(key=lambda x: x if x > 32768 else x + 65536)
						else:
							seqs.sort()

						# Khoảng cách giữa gói đầu và gói cuối sau khi đã sort
						expected = seqs[-1] - seqs[0] + 1
						
						if len(entry['fragments']) == expected:
							# Nối các mảnh theo đúng thứ tự (lấy modulo 65536 để an toàn gọi key gốc)
							assembled = b''.join(entry['fragments'][s % 65536] for s in seqs)
							
							print(f"[RX] ts={ts} | {expected:3d} frags | {len(assembled):6d} bytes — COMPLETE")
							
							if not self.frameQueue.full():
								self.frameQueue.put(assembled)
							del self.reassemblyBuffer[ts]
					# ─────────────────────────────────────────────────────────

			except Exception:
				if self.playEvent.isSet():
					break
				if self.teardownAcked == 1:
					self.rtpSocket.shutdown(socket.SHUT_RDWR)
					self.rtpSocket.close()
					break

	def displayFrame(self):
		"""Main-thread display loop driven by Tkinter's after() scheduler.

		Fetches one fully assembled frame from frameQueue (non-blocking) and
		renders it. Reschedules itself every 50 ms so the GUI remains
		responsive and is never touched from a background thread.
		"""
		try:
			frame_data = self.frameQueue.get_nowait()
			self.updateMovie(self.writeFrame(frame_data))
			
			self.frameNbr += 1
			total_seconds = self.frameNbr // 20
			mins = total_seconds // 60
			secs = total_seconds % 60
			self.timerLabel.configure(text=f"{mins:02d}:{secs:02d}")
		except queue.Empty:
			pass  # Nothing ready yet — reschedule and wait
		finally:
			# Keep the polling loop alive as long as we're playing
			if self.state == self.PLAYING:
				self.master.after(50, self.displayFrame)

	def watchdogThread(self):
		"""Background thread: evict stale (incomplete) frames from the buffer.

		Runs every 200 ms. Any frame whose first fragment arrived more than
		REASSEMBLY_TIMEOUT seconds ago without completing is dropped and
		logged — typically caused by permanent packet loss in the network.
		"""
		while not self.playEvent.isSet():
			time.sleep(0.2)
			now = time.time()
			with self.bufferLock:
				stale = [
					ts for ts, info in self.reassemblyBuffer.items()
					if now - info['arrived'] > REASSEMBLY_TIMEOUT
				]
				for ts in stale:
					entry = self.reassemblyBuffer[ts]
					expected = entry['max_seq'] - entry['min_seq'] + 1
					print(f"[TIMEOUT] ts={ts} dropped — "
					      f"{len(entry['fragments'])}/{expected} frags received")
					del self.reassemblyBuffer[ts]

	def writeFrame(self, data):
		"""Write the received frame to a temp image file. Return the image file."""
		cachename = CACHE_FILE_NAME + str(self.sessionId) + CACHE_FILE_EXT
		with open(cachename, "wb") as f:
			f.write(data)
		return cachename
	
	def updateMovie(self, imageFile):
		"""Update the image file as video frame in the GUI."""
		photo = ImageTk.PhotoImage(Image.open(imageFile))
		self.label.configure(image=photo, height=288)
		self.label.image = photo

	def connectToServer(self):
		"""Connect to the Server. Start a new RTSP/TCP session."""
		self.rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		try:
			self.rtspSocket.connect((self.serverAddr, self.serverPort))
		except:
			tkinter.messagebox.showwarning('Connection Failed',
				'Connection to \'%s\' failed.' % self.serverAddr)
	
	def sendRtspRequest(self, requestCode):
		"""Send RTSP request to the server."""
		if requestCode == self.SETUP and self.state == self.INIT:
			threading.Thread(target=self.recvRtspReply).start()
			self.rtspSeq = 1
			request  = "SETUP " + str(self.fileName) + " RTSP/1.0\n"
			request += "CSeq: " + str(self.rtspSeq) + "\n"
			request += "Transport: RTP/UDP; client_port= " + str(self.rtpPort)
			self.requestSent = self.SETUP

		elif requestCode == self.PLAY and self.state == self.READY:
			self.rtspSeq += 1
			request  = "PLAY " + str(self.fileName) + " RTSP/1.0\n"
			request += "CSeq: " + str(self.rtspSeq) + "\n"
			request += "Session: " + str(self.sessionId)
			self.requestSent = self.PLAY

		elif requestCode == self.PAUSE and self.state == self.PLAYING:
			self.rtspSeq += 1
			request  = "PAUSE " + str(self.fileName) + " RTSP/1.0\n"
			request += "CSeq: " + str(self.rtspSeq) + "\n"
			request += "Session: " + str(self.sessionId)
			self.requestSent = self.PAUSE

		elif requestCode == self.TEARDOWN and not self.state == self.INIT:
			self.rtspSeq += 1
			request  = "TEARDOWN " + str(self.fileName) + " RTSP/1.0\n"
			request += "CSeq: " + str(self.rtspSeq) + "\n"
			request += "Session: " + str(self.sessionId)
			self.requestSent = self.TEARDOWN
		else:
			return

		self.rtspSocket.send(request.encode())
		print('\nData sent:\n' + request)
	
	def recvRtspReply(self):
		"""Receive RTSP reply from the server."""
		while True:
			reply = self.rtspSocket.recv(1024)
			if reply:
				self.parseRtspReply(reply.decode("utf-8"))
			if self.requestSent == self.TEARDOWN:
				self.rtspSocket.shutdown(socket.SHUT_RDWR)
				self.rtspSocket.close()
				break
	
	def parseRtspReply(self, data):
		"""Parse the RTSP reply from the server."""
		lines  = data.split('\n')
		seqNum = int(lines[1].split(' ')[1])
		if seqNum == self.rtspSeq:
			session = int(lines[2].split(' ')[1])
			if self.sessionId == 0:
				self.sessionId = session
			if self.sessionId == session:
				if int(lines[0].split(' ')[1]) == 200:
					if self.requestSent == self.SETUP:
						self.state = self.READY
						self.openRtpPort()
					elif self.requestSent == self.PLAY:
						self.state = self.PLAYING
					elif self.requestSent == self.PAUSE:
						self.state = self.READY
						self.playEvent.set()
					elif self.requestSent == self.TEARDOWN:
						self.state = self.INIT
						self.teardownAcked = 1
					self.master.after(0, self.updateUI)
	
	def openRtpPort(self):
		"""Open RTP socket binded to a specified port."""
		self.rtpSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		self.rtpSocket.settimeout(0.5)
		try:
			self.rtpSocket.bind(('', self.rtpPort))
		except:
			tkinter.messagebox.showwarning('Unable to Bind',
				'Unable to bind PORT=%d' % self.rtpPort)

	def handler(self):
		"""Handler on explicitly closing the GUI window."""
		self.pauseMovie()
		if tkinter.messagebox.askokcancel("Quit?", "Are you sure you want to quit?"):
			self.exitClient()
		else:
			self.playMovie()
