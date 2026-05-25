import sys, socket, select, time
from ServerWorker import ServerWorker

class Server:	
	
	def main(self):
		try:
			SERVER_PORT = int(sys.argv[1])
		except:
			print("[Usage: Server.py Server_port]\n", flush=True)
			sys.exit(1)

		rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		rtspSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		rtspSocket.bind(('', SERVER_PORT))
		rtspSocket.listen(5)        
		rtspSocket.setblocking(0)

		print(f"RTSP Server listening on port {SERVER_PORT} using select() I/O multiplexing...", flush=True)

		workers = {} # Maps connSocket (TCP) -> ServerWorker instance

		try:
			while True:
				# 1. Determine which sockets to monitor for readability
				rlist = [rtspSocket] + list(workers.keys())

				# 2. Determine timeout dynamically based on playing workers' next scheduled frame send time
				now = time.time()
				timeout = None
				playing_workers = [w for w in workers.values() if w.state == ServerWorker.PLAYING]
				if playing_workers:
					earliest_send = min(w.next_send_time for w in playing_workers)
					timeout = max(0.0, earliest_send - now)

				# 3. Call select()
				rlist_ready, _, _ = select.select(rlist, [], [], timeout)

				# 4. Handle new connection
				if rtspSocket in rlist_ready:
					connSocket, clientAddr = rtspSocket.accept()
					connSocket.setblocking(0)
					clientInfo = {'rtspSocket': (connSocket, clientAddr)}
					workers[connSocket] = ServerWorker(clientInfo)
					print(f"Accepted new RTSP connection from {clientAddr}", flush=True)

				# 5. Handle RTSP requests from existing clients
				for connSocket in rlist_ready:
					if connSocket == rtspSocket:
						continue
					worker = workers.get(connSocket)
					if not worker:
						continue
					try:
						# Increased buffer to 1024 bytes to ensure long SWITCH request headers are not truncated
						data = connSocket.recv(1024)
						if data:
							client_addr = worker.clientInfo['rtspSocket'][1]
							print(f"Data received from client {client_addr}:\n" + data.decode("utf-8"), flush=True)
							worker.processRtspRequest(data.decode("utf-8"))
						else:
							# Client disconnected (EOF)
							client_addr = worker.clientInfo['rtspSocket'][1]
							print(f"Client disconnected gracefully: {client_addr}", flush=True)
							worker.cleanup()
							if connSocket in workers:
								del workers[connSocket]
					except Exception as e:
						# Error reading from socket or processing request
						client_addr = worker.clientInfo['rtspSocket'][1] if worker else "unknown"
						print(f"Error handling connection for client {client_addr}: {e}", flush=True)
						if worker:
							worker.cleanup()
						if connSocket in workers:
							del workers[connSocket]

				# 6. Check if any playing worker is due to send the next frame
				now = time.time()
				for worker in list(workers.values()):
					if worker.state == ServerWorker.PLAYING and now >= worker.next_send_time:
						worker.sendNextFrame()
						# Schedule the next frame 50ms from the previous scheduled time to maintain consistent frame rate
						worker.next_send_time = max(now, worker.next_send_time + 0.05)

		except KeyboardInterrupt:
			print("\nServer shutting down...", flush=True)
		finally:
			# Cleanup all active connections
			for worker in list(workers.values()):
				worker.cleanup()
			rtspSocket.close()

if __name__ == "__main__":
	(Server()).main()
