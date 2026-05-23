# Task 1 — RTSP Protocol & RTP Packetization with UDP Fragmentation

## Tổng quan

Task này yêu cầu triển khai hai thành phần cốt lõi của hệ thống streaming video:

1. **RTSP (Real-Time Streaming Protocol)** tại client — dùng TCP để điều khiển phiên streaming (Setup, Play, Pause, Teardown).
2. **RTP (Real-time Transport Protocol) packetization** tại server — dùng UDP để truyền dữ liệu video theo thời gian thực, kèm **fragmentation** để xử lý frame vượt quá MTU.

---

## Kiến trúc tổng thể

```
┌─────────────────────────────────────────────────────────────┐
│                        CLIENT                               │
│                                                             │
│  [GUI Tkinter]                                              │
│       │ button click                                        │
│       ▼                                                     │
│  sendRtspRequest()  ──── TCP ────►  processRtspRequest()   │
│                                         │  SERVER           │
│  recvRtspReply()    ◄─── TCP ────  replyRtsp()             │
│       │                                 │                   │
│       │ (sau PLAY)                      ▼                   │
│       │                          fragmentAndSend()          │
│       ▼                               │ UDP fragments        │
│  listenRtp()        ◄──── UDP ───────┘                     │
│       │                                                     │
│  reassemblyBuffer                                           │
│       │ (frame complete)                                    │
│       ▼                                                     │
│  frameQueue  ──► displayFrame() [main thread, after()]     │
└─────────────────────────────────────────────────────────────┘
```

---

## Phần 1 — RTSP Protocol (Client.py)

### 1.1 Cơ chế hoạt động

RTSP sử dụng kết nối **TCP** để truyền các lệnh điều khiển. Client duy trì một `rtspSeq` (sequence number) để đảm bảo mỗi request/reply được ghép đúng cặp.

**State machine của client:**

```
INIT ──(SETUP)──► READY ──(PLAY)──► PLAYING
                    ▲                   │
                    └────(PAUSE)────────┘
                    │
                 (TEARDOWN)
                    │
                    ▼
                  INIT
```

### 1.2 Gửi RTSP Request — `sendRtspRequest()` ([Client.py, dòng 266])

Hàm này kiểm tra trạng thái hiện tại trước khi xây dựng và gửi request. Bốn loại request được hỗ trợ:

#### SETUP Request
```
SETUP movie.Mjpeg RTSP/1.0
CSeq: 1
Transport: RTP/UDP; client_port= 25000
```
- Gửi khi state = `INIT`
- Thông báo cho server file cần stream và UDP port mà client sẽ lắng nghe RTP
- Khi gọi, đồng thời khởi động thread `recvRtspReply()` để đợi phản hồi

#### PLAY Request
```
PLAY movie.Mjpeg RTSP/1.0
CSeq: 2
Session: 482910
```
- Gửi khi state = `READY`
- Yêu cầu server bắt đầu gửi dữ liệu RTP/UDP

#### PAUSE Request
```
PAUSE movie.Mjpeg RTSP/1.0
CSeq: 3
Session: 482910
```
- Gửi khi state = `PLAYING`
- Yêu cầu server tạm dừng gửi RTP

#### TEARDOWN Request
```
TEARDOWN movie.Mjpeg RTSP/1.0
CSeq: 4
Session: 482910
```
- Gửi khi state ≠ `INIT`
- Kết thúc phiên, đóng cả RTSP socket và RTP socket

### 1.3 Nhận và Parse RTSP Reply — `recvRtspReply()` / `parseRtspReply()` ([Client.py, dòng 302])

Server trả về reply dạng:
```
RTSP/1.0 200 OK
CSeq: 2
Session: 482910
```

`parseRtspReply()` thực hiện:
1. Tách `CSeq` từ reply, đối chiếu với `self.rtspSeq` hiện tại → bỏ qua reply cũ/lạc
2. Đối chiếu `Session ID` → bảo vệ chống nhầm phiên
3. Nếu status code = `200` → chuyển state tương ứng:
   - `SETUP` → state = `READY`, mở RTP socket (`openRtpPort()`)
   - `PLAY`  → state = `PLAYING`
   - `PAUSE` → state = `READY`, set `playEvent` để dừng thread listenRtp
   - `TEARDOWN` → state = `INIT`, set cờ `teardownAcked = 1`

### 1.4 Mở cổng UDP nhận RTP — `openRtpPort()` ([Client.py, dòng 335])

```python
self.rtpSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
self.rtpSocket.settimeout(0.5)   # timeout 0.5s để có thể thoát vòng lặp
self.rtpSocket.bind(('', self.rtpPort))
```

Tạo UDP socket và bind vào port do người dùng chỉ định (ví dụ: 25000). Timeout 0.5 giây cho phép vòng lặp `listenRtp()` kiểm tra cờ thoát mà không bị block mãi mãi.

---

## Phần 2 — RTP Packetization (ServerWorker.py + RtpPacket.py)

### 2.1 Cấu trúc RTP Header (RtpPacket.py)

Mỗi RTP packet gồm **12 bytes header cố định** theo RFC 3550:

```
 0                   1                   2                   3
 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|V=2|P|X|  CC   |M|     PT      |       Sequence Number         |  ← bytes 0–3
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                           Timestamp                           |  ← bytes 4–7
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|           Synchronization Source (SSRC) identifier           |  ← bytes 8–11
+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+-+
|                    Payload (JPEG chunk)                       |  ← bytes 12+
```

| Field | Giá trị | Ý nghĩa |
|-------|---------|---------|
| `V` (version) | `2` | RTP version 2 |
| `P` (padding) | `0` | Không padding |
| `X` (extension) | `0` | Không extension header |
| `CC` (CSRC count) | `0` | Không CSRC |
| `M` (marker) | `0` hoặc `1` | **`1` = fragment cuối của frame** |
| `PT` (payload type) | `26` | MJPEG (RFC 2435) |
| Sequence Number | 16-bit, tăng dần | Đánh số từng packet |
| Timestamp | 32-bit, 90 kHz clock | **Dùng chung cho mọi fragment của 1 frame** |
| SSRC | `0` | Single source, không dùng mixing |

### 2.2 Encode và Decode — `RtpPacket.encode()` / `decode()` ([RtpPacket.py, dòng 11])

**`encode()`** nhận tất cả trường header + payload, đóng gói thành bytearray:

```python
header[0] = (version << 6) | (padding << 5) | (extension << 4) | cc
header[1] = (marker  << 7) | pt          # M bit tại bit cao nhất
header[2] = (seqnum >> 8) & 0xFF         # Sequence number (high byte)
header[3] =  seqnum       & 0xFF         # Sequence number (low byte)
header[4..7] = timestamp  (big-endian)   # 32-bit timestamp
header[8..11] = ssrc      (big-endian)   # 32-bit SSRC
```

> **Điểm quan trọng:** `timestamp` là tham số tùy chọn. Nếu không truyền vào, dùng `int(time())`. Khi server fragment frame, truyền cùng một `frame_ts` cho mọi lần gọi → tất cả fragment cùng frame có timestamp đồng nhất.

**`decode()`** tách 12 byte đầu thành header, phần còn lại thành payload.

**`marker()`** ([dòng 70]) đọc M bit:
```python
return int((self.header[1] >> 7) & 0x01)
```

### 2.3 Xử lý RTSP và điều phối luồng — `processRtspRequest()` ([ServerWorker.py, dòng 47])

Server phân tích request text, điều phối theo loại:

| Request | Hành động |
|---------|-----------|
| `SETUP` | Mở `VideoStream(filename)`, tạo session ID ngẫu nhiên, lưu RTP port của client |
| `PLAY` | Tạo UDP socket, tạo `threading.Event`, khởi động thread `sendRtp()` |
| `PAUSE` | Set event → thread `sendRtp()` thoát vòng lặp |
| `TEARDOWN` | Set event + đóng RTP socket |

---

## Phần 3 — UDP Fragmentation (ServerWorker.py + Client.py)

### 3.1 Vấn đề MTU

JPEG frame trong `movie.Mjpeg` có kích thước thay đổi, thường từ vài KB đến hàng chục KB. MTU của Ethernet là **1500 bytes**. Sau khi trừ IP header (20B) và UDP header (8B):

```
Payload tối đa an toàn = 1500 - 20 - 8 = 1472 bytes
Sau RTP header (12B)  → MAX_RTP_PAYLOAD = 1400 - 12 = 1388 bytes  (conservative)
```

Nếu gửi nguyên frame trong một UDP datagram, hệ điều hành phải tự phân mảnh ở IP layer — dẫn đến mất mát không kiểm soát được. Giải pháp: **application-level fragmentation**.

### 3.2 Server: Phân mảnh frame — `fragmentAndSend()` ([ServerWorker.py, dòng 139])

```python
def fragmentAndSend(self, data, address, port):
    total_len   = len(data)
    total_frags = math.ceil(total_len / MAX_RTP_PAYLOAD)

    # Một timestamp 90kHz duy nhất cho toàn bộ frame
    frame_ts = int(time() * 90000) & 0xFFFFFFFF

    for idx in range(total_frags):
        chunk  = data[idx * MAX_RTP_PAYLOAD : (idx+1) * MAX_RTP_PAYLOAD]
        marker = 1 if (idx == total_frags - 1) else 0   # M=1 chỉ cho chunk cuối

        packet = self.makeRtp(chunk, self.seqNum, marker, frame_ts)
        self.clientInfo['rtpSocket'].sendto(packet, (address, port))
        self.seqNum = (self.seqNum + 1) & 0xFFFF        # 16-bit wrap-around
```

**Ví dụ với frame 50,000 bytes:**
```
total_frags = ceil(50000 / 1388) = 37 fragments

Fragment  0: seq=100, ts=X, marker=0, len=1388B  → sendto()
Fragment  1: seq=101, ts=X, marker=0, len=1388B  → sendto()
  ...
Fragment 36: seq=136, ts=X, marker=1, len= 332B  → sendto()  ← LAST
```

Tất cả fragment dùng **cùng một `ts=X`** và `marker=1` chỉ xuất hiện ở fragment cuối cùng.

### 3.3 Client: Reassembly — `listenRtp()` ([Client.py, dòng 127])

#### Cấu trúc reassemblyBuffer

```python
self.reassemblyBuffer = {
    rtp_timestamp: {
        'fragments': { seq_num: payload_bytes },  # dict, không phải list
        'marked':    bool,    # True khi nhận được packet marker=1
        'arrived':   float,   # time.time() của fragment đầu tiên (cho watchdog)
    }
}
```

Dùng `dict` thay vì `list` để lưu fragment theo `seq_num` → **xử lý được out-of-order** (UDP không đảm bảo thứ tự).

#### Điều kiện hoàn chỉnh

Mỗi khi nhận một packet mới:

```python
ts      = rtpPacket.timestamp()   # nhóm theo timestamp
seq     = rtpPacket.seqNum()      # vị trí trong nhóm
is_last = rtpPacket.marker() == 1

entry['fragments'][seq] = payload
if is_last:
    entry['marked'] = True

# Kiểm tra đủ fragment chưa:
if entry['marked']:
    seqs = sorted(entry['fragments'].keys())   # sắp xếp lại
    expected = seqs[-1] - seqs[0] + 1         # số seq liên tục cần có
    if len(entry['fragments']) == expected:    # không thiếu seq nào
        assembled = b''.join(entry['fragments'][s] for s in seqs)
        self.frameQueue.put(assembled)
        del self.reassemblyBuffer[ts]
```

**Hai điều kiện phải đồng thời thỏa mãn:**
1. Đã nhận packet có `marker=1` (biết đây là fragment cuối)
2. Số fragment nhận được = khoảng seq từ min đến max (không có gap)

#### Xử lý wrap-around 16-bit ([Client.py, dòng 178])

```python
if max(seqs) - min(seqs) > 32768:
    # Sequence number vượt 65535 → sort có nhận thức về wrap-around
    seqs.sort(key=lambda x: x if x > 32768 else x + 65536)
else:
    seqs.sort()
```

Ví dụ: `[65534, 65535, 0, 1]` → sau sort → `[65534, 65535, 0, 1]` (đúng thứ tự vật lý).

### 3.4 Watchdog Thread — `watchdogThread()` ([Client.py, dòng 222])

Chạy song song với `listenRtp()`, quét buffer mỗi **200ms**:

```python
while not self.playEvent.isSet():
    time.sleep(0.2)
    now = time.time()
    with self.bufferLock:
        stale = [ts for ts, info in self.reassemblyBuffer.items()
                 if now - info['arrived'] > REASSEMBLY_TIMEOUT]  # 1.0 giây
        for ts in stale:
            del self.reassemblyBuffer[ts]   # drop frame bị mất fragment
```

Nếu một fragment bị mất vĩnh viễn (ví dụ: do packet loss), frame đó sẽ không bao giờ hoàn chỉnh. Watchdog dọn sạch buffer sau 1 giây để tránh memory leak và unblock client cho frame tiếp theo.

`bufferLock = threading.Lock()` bảo vệ `reassemblyBuffer` vì hai thread (`listenRtp` và `watchdogThread`) truy cập đồng thời.

---

## Phần 4 — Thread-safe GUI Rendering

### 4.1 Vấn đề

Tkinter **không thread-safe** — gọi `label.configure()` hoặc bất kỳ widget nào từ background thread sẽ gây crash hoặc hành vi không xác định.

### 4.2 Giải pháp: `queue.Queue` + `after()`

**Luồng dữ liệu:**
```
listenRtp (background thread)
    │
    │ frameQueue.put(assembled_bytes)
    ▼
frameQueue  ←── thread-safe Python queue
    │
    │ frameQueue.get_nowait()
    ▼
displayFrame (main/Tk thread, được gọi bởi after())
    │
    ▼
writeFrame() → updateMovie() → label.configure(image=photo)
```

**`displayFrame()`** ([Client.py, dòng 205]):
```python
def displayFrame(self):
    try:
        frame_data = self.frameQueue.get_nowait()   # non-blocking
        self.updateMovie(self.writeFrame(frame_data))
    except queue.Empty:
        pass   # chưa có frame, thử lại sau
    finally:
        if self.state == self.PLAYING:
            self.master.after(50, self.displayFrame)  # lặp lại sau 50ms
```

- `get_nowait()` không block → GUI không bị đứng
- `master.after(50, ...)` lên lịch gọi lại sau 50ms **trên main thread** → hoàn toàn an toàn với Tkinter
- Tự động dừng khi `state != PLAYING`

**`playMovie()`** khởi động vòng lặp này ([Client.py, dòng 125](file:///d:/HCMUS/Ki_2_Nam_3/LTM/Project_01_cq/Project_01/skeleton_python_rtp/python_rtp/Client.py#L125)):
```python
self.master.after(50, self.displayFrame)
```

---

## Tóm tắt các file đã chỉnh sửa

| File | Thay đổi chính |
|------|---------------|
| [RtpPacket.py] | `encode()` nhận tham số `timestamp` tùy chọn; thêm getter `marker()` |
| [ServerWorker.py] | Thêm `fragmentAndSend()` chia frame thành chunks ≤ 1388B; `makeRtp()` nhận `timestamp`; `seqNum` tăng toàn cục với 16-bit wrap-around |
| [Client.py] | `sendRtspRequest()` / `parseRtspReply()` đầy đủ 4 loại lệnh; `listenRtp()` reassemble theo timestamp + seq; `displayFrame()` vòng lặp Tkinter-safe; `watchdogThread()` dọn frame stale |
