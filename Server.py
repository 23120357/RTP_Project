import sys, socket, select, time
from ServerWorker import ServerWorker

def closeWorker(worker, workers, connSocket=None):
	if 'rtpSocket' in worker.clientInfo:
		try: worker.clientInfo['rtpSocket'].close()
		except: pass
	if 'rtpTcpSocket' in worker.clientInfo:
		try: worker.clientInfo['rtpTcpSocket'].shutdown(socket.SHUT_RDWR)
		except: pass
		try: worker.clientInfo['rtpTcpSocket'].close()
		except: pass
	if 'videoStream' in worker.clientInfo and worker.clientInfo['videoStream']:
		try: worker.clientInfo['videoStream'].file.close()
		except: pass
	if 'rtspSocket' in worker.clientInfo and worker.clientInfo['rtspSocket']:
		try: worker.clientInfo['rtspSocket'][0].close()
		except: pass
	if connSocket and connSocket in workers:
		del workers[connSocket]

class Server:	
	
	def main(self):
		try:
			SERVER_PORT = int(sys.argv[1])
		except:
			print("[Usage: Server.py Server_port]\n")
			sys.exit(1)

		rtspSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		rtspSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		rtspSocket.bind(('', SERVER_PORT))
		rtspSocket.listen(5)        
		rtspSocket.setblocking(0)
		print(f"RTSP Server listening on port {SERVER_PORT}...")

		workers = {}

		try:
			while True:
				rlist = [rtspSocket] + list(workers.keys())

				now = time.time()
				timeout = None
				playing_workers = [w for w in workers.values() if w.state == ServerWorker.PLAYING]
				if playing_workers:
					earliest_send = min(w.next_send_time for w in playing_workers)
					timeout = max(0.0, earliest_send - now)

				rlist_ready, _, _ = select.select(rlist, [], [], timeout)

				if rtspSocket in rlist_ready:
					connSocket, clientAddr = rtspSocket.accept()
					connSocket.setblocking(0)
					clientInfo = {'rtspSocket': (connSocket, clientAddr)}
					workers[connSocket] = ServerWorker(clientInfo)
					print(f"Accepted new RTSP connection from {clientAddr}")

				for connSocket in rlist_ready:
					if connSocket == rtspSocket:
						continue
					worker = workers.get(connSocket)
					if not worker:
						continue
					try:
						data = connSocket.recv(1024)
						if data:
							worker.processRtspRequest(data.decode("utf-8"))
						else:
							closeWorker(worker, workers, connSocket)
					except Exception as e:
						client_addr = worker.clientInfo['rtspSocket'][1] if worker else "unknown"
						print(f"Error handling connection for client {client_addr}: {e}")
						closeWorker(worker, workers, connSocket)

				now = time.time()
				for worker in list(workers.values()):
					if worker.state == ServerWorker.PLAYING and now >= worker.next_send_time:
						worker.sendNextFrame()
						worker.next_send_time = max(now, worker.next_send_time + 0.05)

		except KeyboardInterrupt:
			print("\nServer shutting down...")
		finally:
			for connSocket, worker in list(workers.items()):
				closeWorker(worker, workers, connSocket)
			rtspSocket.close()

if __name__ == "__main__":
	(Server()).main()