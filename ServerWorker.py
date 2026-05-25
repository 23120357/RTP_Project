from random import randint
from time import time
import sys, traceback, socket, math

from VideoStream import VideoStream
from RtpPacket import RtpPacket

# ── MTU / Fragmentation constants ──────────────────────────────────────────────
MAX_UDP_PAYLOAD = 1450
RTP_HEADER_SIZE = 12    
MAX_RTP_PAYLOAD = MAX_UDP_PAYLOAD - RTP_HEADER_SIZE 
# ────────────────────────────────────────────────────────────────────────────────

class ServerWorker:
	SETUP    = 'SETUP'
	PLAY     = 'PLAY'
	PAUSE    = 'PAUSE'
	TEARDOWN = 'TEARDOWN'
	SWITCH   = 'SWITCH'
	
	INIT    = 0
	READY   = 1
	PLAYING = 2

	OK_200           = 0
	FILE_NOT_FOUND_404 = 1
	CON_ERR_500      = 2
	
	def __init__(self, clientInfo):
		self.clientInfo = clientInfo
		self.state = self.INIT
		self.seqNum = 0
		self.next_send_time = 0.0
		
	def processRtspRequest(self, data):
		"""Process RTSP requests from client on the main event loop."""
		if not data.strip():
			return
			
		try:
			# Get the request type
			request = data.split('\n')
			line1 = request[0].split(' ')
			requestType = line1[0]
			
			# Get the media file name
			filename = line1[1]
			
			# Get the RTSP sequence number 
			seq = request[1].split(' ')
		except Exception as e:
			print(f"Error parsing RTSP request data: {e}", flush=True)
			return
		
		# Process SETUP request
		if requestType == self.SETUP:
			if self.state == self.INIT:
				print("processing SETUP\n", flush=True)
				
				try:
					self.clientInfo['videoStream'] = VideoStream(filename)
					self.state = self.READY
				except IOError:
					self.replyRtsp(self.FILE_NOT_FOUND_404, seq[1])
					return
				
				# Generate a randomized RTSP session ID
				self.clientInfo['session'] = randint(100000, 999999)
				
				# Read the transport protocol
				transport_line = [l for l in request if "Transport:" in l]
				if transport_line and "TCP" in transport_line[0]:
					self.clientInfo['transport'] = 'TCP'
				else:
					self.clientInfo['transport'] = 'UDP'
				
				# Send RTSP reply
				self.replyRtsp(self.OK_200, seq[1])
				
				# Get the client's RTP port
				self.clientInfo['rtpPort'] = request[2].split(' ')[3]
		
		# Process PLAY request 		
		elif requestType == self.PLAY:
			if self.state == self.READY:
				print("processing PLAY\n", flush=True)
				self.state = self.PLAYING
				self.replyRtsp(self.OK_200, seq[1])
				
				# Schedule the first transmission immediately
				self.next_send_time = time()
		
		# Process PAUSE request
		elif requestType == self.PAUSE:
			if self.state == self.PLAYING:
				print("processing PAUSE\n", flush=True)
				self.state = self.READY
				self.replyRtsp(self.OK_200, seq[1])
		
		# Process SWITCH request (for quality and transport switching)
		elif requestType == self.SWITCH:
			if self.state in [self.READY, self.PLAYING]:
				was_playing = (self.state == self.PLAYING)
				print("processing SWITCH\n", flush=True)
				
				# Close old RTP sockets (UDP and TCP)
				old_transport = self.clientInfo.get('transport', 'UDP')
				if old_transport == 'TCP' and 'rtpTcpSocket' in self.clientInfo:
					try:
						self.clientInfo['rtpTcpSocket'].shutdown(socket.SHUT_RDWR)
					except: pass
					try:
						self.clientInfo['rtpTcpSocket'].close()
					except: pass
					del self.clientInfo['rtpTcpSocket']
				elif old_transport == 'UDP' and 'rtpSocket' in self.clientInfo:
					try:
						self.clientInfo['rtpSocket'].close()
					except: pass
					del self.clientInfo['rtpSocket']
				
				# Retrieve frame number to skip to
				current_frame = 0
				client_frame_line = [l for l in request if "ClientFrame:" in l]
				if client_frame_line:
					current_frame = int(client_frame_line[0].split(':')[1].strip())
				elif 'videoStream' in self.clientInfo:
					current_frame = self.clientInfo['videoStream'].frameNbr()
				
				# Close the current VideoStream
				if 'videoStream' in self.clientInfo and self.clientInfo['videoStream']:
					try:
						self.clientInfo['videoStream'].file.close()
					except: pass
				
				# Open new VideoStream and skip to target frame
				try:
					self.clientInfo['videoStream'] = VideoStream(filename)
					self.clientInfo['videoStream'].skipToFrame(current_frame)
				except IOError:
					self.replyRtsp(self.FILE_NOT_FOUND_404, seq[1])
					return
				
				# Update the transport protocol
				transport_line = [l for l in request if "Transport:" in l]
				new_transport = 'TCP' if (transport_line and "TCP" in transport_line[0]) else 'UDP'
				self.clientInfo['transport'] = new_transport
				
				# Send RTSP reply
				self.replyRtsp(self.OK_200, seq[1])
				
				# If we were playing, schedule immediate next frame transmission
				if was_playing:
					self.next_send_time = time()
		
		# Process TEARDOWN request
		elif requestType == self.TEARDOWN:
			print("processing TEARDOWN\n", flush=True)
			self.replyRtsp(self.OK_200, seq[1])
			self.cleanup()
			self.state = self.INIT

	def sendNextFrame(self):
		"""Send the next video frame over UDP or TCP depending on transport."""
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
				print("Connection Error:", e, flush=True)
				traceback.print_exc(file=sys.stdout)
		else:
			print("End of video stream reached.", flush=True)
			self.state = self.READY

	def fragmentAndSendUDP(self, data, address, port):
		"""Fragment a video frame into MTU-sized RTP packets and transmit over UDP."""
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
			self.seqNum = (self.seqNum + 1) & 0xFFFF  # 16-bit wrap-around

	def sendTCP(self, data, address, port):
		"""Transmit a video frame as a single RTP packet prefixed by length over TCP."""
		if 'rtpTcpSocket' not in self.clientInfo:
			try:
				self.clientInfo['rtpTcpSocket'] = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
				self.clientInfo['rtpTcpSocket'].connect((address, port))
			except Exception:
				return
		
		frame_ts = int(time() * 90000) & 0xFFFFFFFF
		packet = self.makeRtp(data, self.seqNum, marker=1, timestamp=frame_ts)
		self.seqNum = (self.seqNum + 1) & 0xFFFF  # 16-bit wrap-around
		
		length_prefix = len(packet).to_bytes(4, byteorder='big')
		try:
			self.clientInfo['rtpTcpSocket'].sendall(length_prefix + packet)
		except Exception:
			# If socket closed or error, close and clean up to retry next frame
			try:
				self.clientInfo['rtpTcpSocket'].close()
			except: pass
			if 'rtpTcpSocket' in self.clientInfo:
				del self.clientInfo['rtpTcpSocket']

	def makeRtp(self, payload, seqnum, marker=0, timestamp=None):
		"""RTP-packetize a payload chunk."""
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
		elif code == self.FILE_NOT_FOUND_404:
			print("404 NOT FOUND", flush=True)
		elif code == self.CON_ERR_500:
			print("500 CONNECTION ERROR", flush=True)

	def cleanup(self):
		"""Gracefully release all resources associated with this client connection."""
		session_id = self.clientInfo.get('session', 'N/A')
		print(f"Cleaning up resources for session {session_id}", flush=True)
		
		# Close RTSP TCP socket
		if 'rtspSocket' in self.clientInfo and self.clientInfo['rtspSocket']:
			try:
				self.clientInfo['rtspSocket'][0].close()
			except: pass
			
		# Close UDP RTP socket
		if 'rtpSocket' in self.clientInfo and self.clientInfo['rtpSocket']:
			try:
				self.clientInfo['rtpSocket'].close()
			except: pass
			
		# Close TCP RTP socket
		if 'rtpTcpSocket' in self.clientInfo and self.clientInfo['rtpTcpSocket']:
			try:
				self.clientInfo['rtpTcpSocket'].shutdown(socket.SHUT_RDWR)
			except: pass
			try:
				self.clientInfo['rtpTcpSocket'].close()
			except: pass
			
		# Close video stream file
		if 'videoStream' in self.clientInfo and self.clientInfo['videoStream']:
			try:
				self.clientInfo['videoStream'].file.close()
			except: pass