from random import randint
from time import time
import socket, math

from VideoStream import VideoStream
from RtpPacket import RtpPacket

MAX_UDP_PAYLOAD = 1450
RTP_HEADER_SIZE = 12    
MAX_RTP_PAYLOAD = MAX_UDP_PAYLOAD - RTP_HEADER_SIZE 

class ServerWorker:
	SETUP    = 'SETUP'
	PLAY     = 'PLAY'
	PAUSE    = 'PAUSE'
	TEARDOWN = 'TEARDOWN'
	SWITCH   = 'SWITCH'
	
	INIT    = 0
	READY   = 1
	PLAYING = 2

	OK_200             = 0
	FILE_NOT_FOUND_404 = 1
	CON_ERR_500        = 2
	
	def __init__(self, clientInfo):
		self.clientInfo = clientInfo
		self.state = self.INIT
		self.seqNum = 0
		self.next_send_time = 0.0
		
	def processRtspRequest(self, data):
		if not data.strip():
			return
			
		try:
			request = data.split('\n')
			line1 = request[0].split(' ')
			requestType = line1[0]
			filename = line1[1]
			seq = request[1].split(' ')
		except Exception as e:
			print(f"Error parsing RTSP request data: {e}")
			return
		
		if requestType == self.SETUP:
			if self.state == self.INIT:
				try:
					self.clientInfo['videoStream'] = VideoStream(filename)
					self.state = self.READY
				except IOError:
					self.replyRtsp(self.FILE_NOT_FOUND_404, seq[1])
					return
				
				self.clientInfo['session'] = randint(100000, 999999)
				
				transport_line = [l for l in request if "Transport:" in l]
				if transport_line and "TCP" in transport_line[0]:
					self.clientInfo['transport'] = 'TCP'
				else:
					self.clientInfo['transport'] = 'UDP'
				
				self.replyRtsp(self.OK_200, seq[1])
				self.clientInfo['rtpPort'] = request[2].split(' ')[3]
		
		elif requestType == self.PLAY:
			if self.state == self.READY:
				self.state = self.PLAYING
				self.replyRtsp(self.OK_200, seq[1])
				self.next_send_time = time()
		
		elif requestType == self.PAUSE:
			if self.state == self.PLAYING:
				self.state = self.READY
				self.replyRtsp(self.OK_200, seq[1])
		
		elif requestType == self.SWITCH:
			if self.state in [self.READY, self.PLAYING]:
				was_playing = (self.state == self.PLAYING)
				
				old_transport = self.clientInfo.get('transport', 'UDP')
				if old_transport == 'TCP' and 'rtpTcpSocket' in self.clientInfo:
					try: self.clientInfo['rtpTcpSocket'].shutdown(socket.SHUT_RDWR)
					except: pass
					try: self.clientInfo['rtpTcpSocket'].close()
					except: pass
					del self.clientInfo['rtpTcpSocket']
				elif old_transport == 'UDP' and 'rtpSocket' in self.clientInfo:
					try: self.clientInfo['rtpSocket'].close()
					except: pass
					del self.clientInfo['rtpSocket']
				
				current_frame = 0
				client_frame_line = [l for l in request if "ClientFrame:" in l]
				if client_frame_line:
					current_frame = int(client_frame_line[0].split(':')[1].strip())
				elif 'videoStream' in self.clientInfo:
					current_frame = self.clientInfo['videoStream'].frameNbr()
				
				if 'videoStream' in self.clientInfo and self.clientInfo['videoStream']:
					try: self.clientInfo['videoStream'].file.close()
					except: pass
				
				try:
					self.clientInfo['videoStream'] = VideoStream(filename)
					self.clientInfo['videoStream'].skipToFrame(current_frame)
				except IOError:
					self.replyRtsp(self.FILE_NOT_FOUND_404, seq[1])
					return
				
				transport_line = [l for l in request if "Transport:" in l]
				new_transport = 'TCP' if (transport_line and "TCP" in transport_line[0]) else 'UDP'
				self.clientInfo['transport'] = new_transport
				
				self.replyRtsp(self.OK_200, seq[1])
				
				if was_playing:
					self.next_send_time = time()
		
		elif requestType == self.TEARDOWN:
			self.replyRtsp(self.OK_200, seq[1])
			
			if 'rtspSocket' in self.clientInfo and self.clientInfo['rtspSocket']:
				try: self.clientInfo['rtspSocket'][0].close()
				except: pass
			if 'rtpSocket' in self.clientInfo:
				try: self.clientInfo['rtpSocket'].close()
				except: pass
			if 'rtpTcpSocket' in self.clientInfo:
				try: self.clientInfo['rtpTcpSocket'].shutdown(socket.SHUT_RDWR)
				except: pass
				try: self.clientInfo['rtpTcpSocket'].close()
				except: pass
			if 'videoStream' in self.clientInfo and self.clientInfo['videoStream']:
				try: self.clientInfo['videoStream'].file.close()
				except: pass
			
			self.state = self.INIT

	def sendNextFrame(self):
		if self.state != self.PLAYING:
			return

		data = self.clientInfo['videoStream'].nextFrame()
		if data:
			try:
				address = self.clientInfo['rtspSocket'][1][0]
				port    = int(self.clientInfo['rtpPort'])
				
				transport = self.clientInfo.get('transport', 'UDP')
				if transport == 'UDP':
					self.fragmentAndSendUDP(data, address, port)
				else:
					self.sendTCP(data, address, port)
			except Exception as e:
				print("Connection Error:", e)
		else:
			print("End of video stream reached.")
			self.state = self.READY

	def fragmentAndSendUDP(self, data, address, port):
		if 'rtpSocket' not in self.clientInfo:
			self.clientInfo['rtpSocket'] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

		total_len   = len(data)
		total_frags = math.ceil(total_len / MAX_RTP_PAYLOAD)
		frame_ts = int(time() * 90000) & 0xFFFFFFFF

		for idx in range(total_frags):
			start   = idx * MAX_RTP_PAYLOAD
			chunk   = data[start : start + MAX_RTP_PAYLOAD]
			marker  = 1 if (idx == total_frags - 1) else 0

			packet = self.makeRtp(chunk, self.seqNum, marker, frame_ts)
			if 'rtpSocket' in self.clientInfo:
				self.clientInfo['rtpSocket'].sendto(packet, (address, port))
			self.seqNum = (self.seqNum + 1) & 0xFFFF

	def sendTCP(self, data, address, port):
		if 'rtpTcpSocket' not in self.clientInfo:
			try:
				self.clientInfo['rtpTcpSocket'] = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
				self.clientInfo['rtpTcpSocket'].connect((address, port))
			except Exception:
				return
		
		frame_ts = int(time() * 90000) & 0xFFFFFFFF
		packet = self.makeRtp(data, self.seqNum, marker=1, timestamp=frame_ts)
		self.seqNum = (self.seqNum + 1) & 0xFFFF
		
		length_prefix = len(packet).to_bytes(4, byteorder='big')
		try:
			self.clientInfo['rtpTcpSocket'].sendall(length_prefix + packet)
		except Exception:
			try: self.clientInfo['rtpTcpSocket'].close()
			except: pass
			if 'rtpTcpSocket' in self.clientInfo:
				del self.clientInfo['rtpTcpSocket']

	def makeRtp(self, payload, seqnum, marker=0, timestamp=None):
		version   = 2
		padding   = 0
		extension = 0
		cc        = 0
		pt        = 26
		ssrc      = 0

		rtpPacket = RtpPacket()
		rtpPacket.encode(version, padding, extension, cc, seqnum, marker, pt, ssrc,
		                 payload, timestamp)
		return rtpPacket.getPacket()
		
	def replyRtsp(self, code, seq):
		if code == self.OK_200:
			reply = 'RTSP/1.0 200 OK\nCSeq: ' + seq + '\nSession: ' + str(self.clientInfo['session'])
			connSocket = self.clientInfo['rtspSocket'][0]
			connSocket.send(reply.encode())