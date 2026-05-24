import sys
from tkinter import Tk
from Client import Client

if __name__ == "__main__":
	if len(sys.argv) < 4:
		print("[Usage: ClientLauncher.py Server_name Server_port RTP_port]\n")
		sys.exit(1)

	serverAddr = sys.argv[1]
	serverPort = int(sys.argv[2])
	rtpPort = int(sys.argv[3])
	
	root = Tk()
	
	# Force exact dimensions and completely disable resizing to block OS auto-maximize
	root.geometry("680x500") 
	root.resizable(False, False)
	root.title("RTP Client Video Stream")
	
	app = Client(root, serverAddr, serverPort, rtpPort)
	app.master.title("RTPClient")	
	root.mainloop()