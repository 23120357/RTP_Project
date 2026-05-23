from random import randint
from time import time
import sys, traceback, threading, socket, math

from VideoStream import VideoStream
from RtpPacket import RtpPacket

# ── MTU / Fragmentation constants ──────────────────────────────────────────────
MAX_UDP_PAYLOAD = 1400  # bytes — conservative, fits inside Ethernet MTU
RTP_HEADER_SIZE = 12    # bytes — standard fixed RTP header
MAX_RTP_PAYLOAD = MAX_UDP_PAYLOAD - RTP_HEADER_SIZE  # 1388 bytes per fragment
# ────────────────────────────────────────────────────────────────────────────────

class ServerWorker:
	SETUP    = 'SETUP'
	PLAY     = 'PLAY'
	PAUSE    = 'PAUSE'
	TEARDOWN = 'TEARDOWN'
	
	INIT    = 0
	READY   = 1
	PLAYING = 2
	state   = INIT

	OK_200           = 0
	FILE_NOT_FOUND_404 = 1
	CON_ERR_500      = 2
	
	clientInfo = {}
	
	def __init__(self, clientInfo):
		self.clientInfo = clientInfo
		self.seqNum = 0   # Global RTP sequence number — increments per fragment (16-bit)
		
	def run(self):
		threading.Thread(target=self.recvRtspRequest).start()
	
	def recvRtspRequest(self):
		"""Receive RTSP request from the client."""
		connSocket = self.clientInfo['rtspSocket'][0]
		while True:            
			data = connSocket.recv(256)
			if data:
				print("Data received:\n" + data.decode("utf-8"))
				self.processRtspRequest(data.decode("utf-8"))
	
	def processRtspRequest(self, data):
		"""Process RTSP request sent from the client."""
		# Get the request type
		request = data.split('\n')
		line1 = request[0].split(' ')
		requestType = line1[0]
		
		# Get the media file name
		filename = line1[1]
		
		# Get the RTSP sequence number 
		seq = request[1].split(' ')
		
		# Process SETUP request
		if requestType == self.SETUP:
			if self.state == self.INIT:
				# Update state
				print("processing SETUP\n")
				
				try:
					self.clientInfo['videoStream'] = VideoStream(filename)
					self.state = self.READY
				except IOError:
					self.replyRtsp(self.FILE_NOT_FOUND_404, seq[1])
				
				# Generate a randomized RTSP session ID
				self.clientInfo['session'] = randint(100000, 999999)
				
				# Send RTSP reply
				self.replyRtsp(self.OK_200, seq[1])
				
				# Get the RTP/UDP port from the last line
				self.clientInfo['rtpPort'] = request[2].split(' ')[3]
		
		# Process PLAY request 		
		elif requestType == self.PLAY:
			if self.state == self.READY:
				print("processing PLAY\n")
				self.state = self.PLAYING
				
				# Create a new socket for RTP/UDP
				self.clientInfo["rtpSocket"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
				
				self.replyRtsp(self.OK_200, seq[1])
				
				# Create a new thread and start sending RTP packets
				self.clientInfo['event'] = threading.Event()
				self.clientInfo['worker'] = threading.Thread(target=self.sendRtp) 
				self.clientInfo['worker'].start()
		
		# Process PAUSE request
		elif requestType == self.PAUSE:
			if self.state == self.PLAYING:
				print("processing PAUSE\n")
				self.state = self.READY
				
				self.clientInfo['event'].set()
			
				self.replyRtsp(self.OK_200, seq[1])
		
		# Process TEARDOWN request
		elif requestType == self.TEARDOWN:
			print("processing TEARDOWN\n")

			self.clientInfo['event'].set()
			
			self.replyRtsp(self.OK_200, seq[1])
			
			# Close the RTP socket
			self.clientInfo['rtpSocket'].close()

	def sendRtp(self):
		"""Send RTP packets over UDP — uses fragmentAndSend() for MTU compliance."""
		while True:
			self.clientInfo['event'].wait(0.05)
			
			# Stop sending if request is PAUSE or TEARDOWN
			if self.clientInfo['event'].isSet():
				break
				
			data = self.clientInfo['videoStream'].nextFrame()
			if data:
				try:
					address = self.clientInfo['rtspSocket'][1][0]
					port    = int(self.clientInfo['rtpPort'])
					self.fragmentAndSend(data, address, port)
				except Exception as e:
					print("Connection Error:", e)
					print('-'*60)
					traceback.print_exc(file=sys.stdout)
					print('-'*60)

	def fragmentAndSend(self, data, address, port):
		"""Fragment a video frame into MTU-sized RTP packets and transmit over UDP.

		Standard RTP fields only — no custom application header:
		  • Timestamp : one value shared by every fragment of this frame
		               (90 kHz clock, as per RFC 2435 for MJPEG)
		  • Sequence  : increments globally across all packets
		  • Marker    : set to 1 on the last fragment only (signals frame boundary
		               to the receiver, per RFC 3550 §5.3)
		"""
		total_len   = len(data)
		total_frags = math.ceil(total_len / MAX_RTP_PAYLOAD)

		# Single 90 kHz RTP timestamp for the entire frame (32-bit wrap-around)
		frame_ts = int(time() * 90000) & 0xFFFFFFFF

		for idx in range(total_frags):
			start   = idx * MAX_RTP_PAYLOAD
			chunk   = data[start : start + MAX_RTP_PAYLOAD]
			marker  = 1 if (idx == total_frags - 1) else 0

			packet = self.makeRtp(chunk, self.seqNum, marker, frame_ts)
			self.clientInfo['rtpSocket'].sendto(packet, (address, port))
			self.seqNum = (self.seqNum + 1) & 0xFFFF  # 16-bit wrap-around

		print(f"[TX] ts={frame_ts} | {total_frags:3d} frags | {total_len:6d} bytes")

	def makeRtp(self, payload, seqnum, marker=0, timestamp=None):
		"""RTP-packetize a payload chunk.
		
		Passing an explicit *timestamp* ensures every fragment of the same
		video frame carries an identical value for client-side reassembly.
		"""
		version   = 2
		padding   = 0
		extension = 0
		cc        = 0
		pt        = 26  # MJPEG payload type
		ssrc      = 0

		rtpPacket = RtpPacket()
		rtpPacket.encode(version, padding, extension, cc, seqnum, marker, pt, ssrc,
		                 payload, timestamp)
		return rtpPacket.getPacket()
		
	def replyRtsp(self, code, seq):
		"""Send RTSP reply to the client."""
		if code == self.OK_200:
			reply = 'RTSP/1.0 200 OK\nCSeq: ' + seq + '\nSession: ' + str(self.clientInfo['session'])
			connSocket = self.clientInfo['rtspSocket'][0]
			connSocket.send(reply.encode())
		
		# Error messages
		elif code == self.FILE_NOT_FOUND_404:
			print("404 NOT FOUND")
		elif code == self.CON_ERR_500:
			print("500 CONNECTION ERROR")
