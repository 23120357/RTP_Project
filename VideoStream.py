class VideoStream:
	def __init__(self, filename):
		self.filename = filename
		try:
			self.file = open(filename, 'rb')
		except:
			raise IOError
		self.frameNum = 0
		
	def nextFrame(self):
		"""Đọc frame và tự động xử lý độ dài header động nếu kích thước ảnh lớn hơn 5 chữ số"""
		header = self.file.read(5) 
		if not header: 
			return None 
			
		try:
			framelength = int(header)
		except ValueError:
			return None
						
		# Đọc vòng lặp phòng trường hợp độ dài byte ảnh HD lấn sang ký tự tiếp theo
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
		
	def frameNbr(self):
		return self.frameNum
	
	def skipToFrame(self, targetFrameNum):
		while self.frameNum < targetFrameNum:
			frame = self.nextFrame()
			if not frame:
				break