# 🚀 Tài liệu Kiến trúc V2: Nâng cấp Hệ thống Streaming Đa phân giải (SD/HD/FHD)

Tài liệu này mô tả chi tiết các thay đổi về mặt kiến trúc và logic code được triển khai trong phiên bản mới nhất (`Client.py`, `ServerWorker.py`, `VideoStream.py`). 

Mục tiêu của bản cập nhật này là giải quyết triệt để vấn đề rớt gói tin (packet loss) khi truyền ảnh độ phân giải cao qua UDP, tối ưu hóa hiệu năng CPU/RAM và cho phép chuyển đổi chất lượng video mượt mà (Seamless Switching) theo thời gian thực.
```
===================================================================================
                           CLIENT MULTI-THREADING ARCHITECTURE (FIXED)
===================================================================================

[ SERVER ]
    |
    | (1) RTSP Control (TCP)
    |--------------------------------------------> [ 1. MAIN / GUI THREAD ]
    |                                                - Quản lý giao diện Tkinter
    |                                                - sendRtspRequest()
    |                                                - Hàm after(): displayFrame()
    |
    | (2) SD Video (UDP)                             [ 2. UDP RECEIVER THREAD ]
    |--------------------------------------------> rtpUdpSocket.recv()
    |                                                |-> Ráp các mảnh UDP
    |                                                v
    |                                      +-------------------------------+
    |                                      |    frameCache (RAM Queue)     |
    |                                      |      [ ][ ][ ][ ][ ][ ]       |
    | (3) HD/FHD Video (TCP)               |  (Tự động Block luồng khi đầy)|
    |--+                                   +-------------------------------+
       |                                                 ^
       v                                                 | (Đẩy thẳng vào Queue)
    +--------------------------+                         |
    | 3. TCP ACCEPTOR THREAD   |                         |
    | rtpTcpListener.accept()  |                         |
    +-------------+------------+                         |
                  | (Tạo luồng nhận)                     |
                  v                                      |
         [ 4. TCP RECEIVER THREAD ]                      |
         rtpTcpSocket.recvall() -------------------------+
                                                         
                                                         | (Rút ảnh mỗi 50ms)
                                                         v
                                               [ 1. MAIN / GUI THREAD ]
                                               - io.BytesIO(frame_data)
                                               - Image.resize(BILINEAR)
                                               - Label.configure(image)

===================================================================================
 [ 5. WATCHDOG THREAD ] -> Chạy ngầm, quét & dọn dẹp reassemblyBuffer mỗi 200ms
===================================================================================
```

---

## 1. Giao thức Truyền tải Kép (Dual Transport: TCP/UDP)
Trong phiên bản cũ, chúng ta chỉ sử dụng UDP (với Application-level Fragmentation) cho chất lượng SD. Khi nâng lên HD/FHD, kích thước một frame có thể vượt quá 100-200KB, việc chia nhỏ thành hàng trăm mảnh UDP dẫn đến tỷ lệ hỏng frame cực cao.

**Giải pháp V2:**
Hệ thống tự động phân luồng giao thức dựa trên độ phân giải:
* **SD (960x540):** Tiếp tục sử dụng luồng UDP để tối ưu độ trễ.
* **HD (1280x720) & FHD (1920x1080):** Chuyển sang sử dụng **TCP** nhằm tận dụng cơ chế truyền tải tin cậy 100% của hệ điều hành.

### Triển khai TCP Framing (Chống lỗi Sticky Payload)
TCP là dạng luồng byte liên tục (Byte Stream), không có ranh giới gói tin. Để Client biết đâu là điểm kết thúc của một bức ảnh, chúng ta áp dụng cơ chế **Length-Prefix Framing**:
* **Tại Server (`ServerWorker.py`):** Gắn thêm `4 bytes` (Big Endian) chỉ định độ dài của gói RTP ngay trước dữ liệu thực tế.
  ```python
  length_prefix = len(packet).to_bytes(4, byteorder='big')
  self.clientInfo['rtpTcpSocket'].sendall(length_prefix + packet)

2\. Kiểm soát lưu lượng tự động (OS-Level Backpressure)
-------------------------------------------------------

Một vấn đề thường gặp ở streaming TCP là Server đẩy dữ liệu quá nhanh làm tràn bộ đệm RAM của Client. V2 giải quyết điều này hoàn toàn tự động, không cần code thêm vòng lặp kiểm tra rườm rà.

**Luồng hoạt động của Backpressure:**

1.  Hàng đợi self.frameCache tại Client được giới hạn nhỏ lại (maxsize=20).
    
2.  Khi bộ đệm đầy, lệnh self.frameCache.put() trong thread listenRtpTCP sẽ bị treo (Block).
    
3.  Do thread bị treo, Client ngừng gọi recv(), dẫn đến bộ đệm TCP của hệ điều hành nhanh chóng bị đầy.
    
4.  Hệ điều hành Client tự động báo TCP Window Size = 0 cho Server.
    
5.  Lệnh sendall() bên Server tự động bị hệ điều hành "khóa cứng" (Block).
    
6.  Server ngừng đẩy dữ liệu cho đến khi Client tiêu thụ bớt frame và giải phóng không gian.
    

_Kết quả: Băng thông được điều tiết chuẩn xác, RAM không bao giờ tràn, và không mất bất kỳ một frame nào._

3\. Chuyển đổi chất lượng mượt mà (Seamless Quality Switching)
--------------------------------------------------------------

Người dùng có thể bấm Radiobutton (SD/HD/FHD) để đổi chất lượng giữa chừng mà không làm đứt gãy luồng thời gian (Time-shift bug).

**Cơ chế đồng bộ Client-Driven:** Do tốc độ mạng nhanh hơn tốc độ chiếu, Server luôn chạy trước Client một đoạn. Để tránh việc đổi chất lượng làm video bị nhảy cóc (Skip frames), Client phải báo cho Server biết nó đang chiếu tới đâu.

*   Khi gửi lệnh SWITCH qua RTSP, Client đính kèm Header: ClientFrame: .
    
*   Khi Server nhận lệnh SWITCH, nó sẽ parse Header này, mở file Mjpeg có chất lượng tương ứng và gọi hàm skipToFrame(current\_frame) để tua đến đúng vị trí Client đang xem.
    

**Xử lý Deadlock khi chuyển đổi TCP:** Do cơ chế Backpressure ở trên, khi người dùng bấm SWITCH, thread gửi dữ liệu của Server có thể đang bị treo tại sendall().

*   **Fix (ServerWorker.py):** Trước khi gọi worker.join(), Server phải gọi socket.shutdown() và close() đường truyền cũ. Việc sập socket đột ngột sẽ ném ra Exception, đập vỡ trạng thái Blocking, giúp thread Server có thể thoát ra an toàn và khởi động luồng mới.
    

4\. Tối ưu hóa Hiệu năng hiển thị (Xóa bỏ Thắt cổ chai)
-------------------------------------------------------

Phiên bản cũ gặp lỗi video FHD bị phát dưới dạng "slow-motion" (< 15 FPS) do CPU quá tải và ổ cứng không đáp ứng kịp. V2 đã thiết kế lại hoàn toàn hàm displayFrame trong Client2.py:

*   **Bypass Disk I/O:** Bỏ việc ghi file cache.jpg. Thay vào đó, dữ liệu lấy từ hàng đợi được nạp thẳng vào RAM thông qua io.BytesIO.
    
*   **Đổi thuật toán Resize:** Đổi từ thuật toán LANCZOS (rất nặng) sang BILINEAR. Việc thu phóng ảnh FHD trên UI giờ đây chỉ tốn vài mili-giây.
 ```
process_time = int((time.time() - start_time) * 1000)
wait_time = max(1, 50 - process_time)
self.master.after(wait_time, self.displayFrame)
 ```

Nhờ đó, tốc độ hiển thị luôn được khóa chặt ở **20 FPS**, hình ảnh mượt mà cho dù phần cứng của thiết bị Client mạnh hay yếu.
    

5\. Parser Header động (Dynamic Video Header Parsing)
-----------------------------------------------------

File video tùy biến định dạng .Mjpeg lưu độ dài mỗi bức ảnh bằng 5 byte text ở đầu. Tuy nhiên, một số bức ảnh FHD có kích thước vượt quá 99.999 byte (cần 6 chữ số), khiến logic cũ bị sai lệch (đọc mất một byte của dữ liệu ảnh).

**Cải tiến tại VideoStream.py:** Hàm nextFrame() sử dụng vòng lặp kiểm tra từng byte tiếp theo:
```
while True:
    next_byte = self.file.read(1)
    if not next_byte: break
    if next_byte.isdigit():
        framelength = framelength * 10 + int(next_byte)
    else:
        # Gặp ký tự không phải số -> Đó là byte đầu tiên của ảnh
        img_data = self.file.read(framelength - 1)
        return next_byte + img_data
```