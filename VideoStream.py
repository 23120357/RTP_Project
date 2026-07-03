class VideoStream:
	def __init__(self, filename):
		self.filename = filename
		try:
			self.file = open(filename, 'rb')
		except:
			raise IOError
		self.frameNum = 0
		self.is_raw_mjpeg = None
		self.read_buffer = b""
		
	def nextFrame(self):
		"""Đọc frame và tự động phát hiện định dạng:
		- Định dạng có header độ dài 5+ chữ số (custom)
		- Định dạng MJPEG chuẩn (các ảnh JPEG nối tiếp nhau bắt đầu bằng \\xff\\xd8 và kết thúc bằng \\xff\\xd9)
		"""
		if self.is_raw_mjpeg is None:
			header = self.file.read(5)
			if not header:
				return None
			
			if header.isdigit():
				self.is_raw_mjpeg = False
				try:
					framelength = int(header)
				except ValueError:
					return None
				
				while True:
					next_byte = self.file.read(1)
					if not next_byte:
						break
					if next_byte.isdigit():
						framelength = framelength * 10 + int(next_byte)
					else:
						img_data = self.file.read(framelength - 1)
						self.frameNum += 1
						return next_byte + img_data
				
				data = self.file.read(framelength)
				self.frameNum += 1
				return data
			else:
				self.is_raw_mjpeg = True
				self.read_buffer = header

		if self.is_raw_mjpeg:
			while True:
				if len(self.read_buffer) < 2:
					chunk = self.file.read(65536)
					if not chunk:
						if len(self.read_buffer) > 0:
							data = self.read_buffer
							self.read_buffer = b""
							self.frameNum += 1
							return data
						return None
					self.read_buffer += chunk
					continue

				if not self.read_buffer.startswith(b'\xff\xd8'):
					idx = self.read_buffer.find(b'\xff\xd8')
					if idx == -1:
						# Đọc tiếp hoặc bỏ qua dữ liệu rác
						self.read_buffer = self.read_buffer[-1:]
						continue
					else:
						self.read_buffer = self.read_buffer[idx:]

				idx = self.read_buffer.find(b'\xff\xd9', 2)
				if idx != -1:
					frame_len = idx + 2
					data = self.read_buffer[:frame_len]
					self.read_buffer = self.read_buffer[frame_len:]
					self.frameNum += 1
					return data

				chunk = self.file.read(65536)
				if not chunk:
					if len(self.read_buffer) > 0:
						data = self.read_buffer
						self.read_buffer = b""
						self.frameNum += 1
						return data
					return None
				self.read_buffer += chunk

	def frameNbr(self):
		return self.frameNum
	
	def skipToFrame(self, targetFrameNum):
		while self.frameNum < targetFrameNum:
			frame = self.nextFrame()
			if not frame:
				break
