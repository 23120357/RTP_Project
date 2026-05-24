import sys, socket, threading

from ServerWorker import ServerWorker

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

		# Receive client info (address,port) through RTSP/TCP session
		while True:
			clientInfo = {}
			clientInfo['rtspSocket'] = rtspSocket.accept()
			worker = ServerWorker(clientInfo)
			# Each client connection runs in its own thread.
			worker_thread = threading.Thread(target=worker.recvRtspRequest)
			worker_thread.daemon = True # Allow main program to exit even if threads are running.
			worker_thread.start()

if __name__ == "__main__":
	(Server()).main()
