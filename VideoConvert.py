import cv2
import os

def create_multi_quality_mjpeg(input_video_path):
    print(f"Đang mở video: {input_video_path}...")
    cap = cv2.VideoCapture(input_video_path)
    
    if not cap.isOpened():
        print("Lỗi: Không thể mở được video gốc. Vui lòng kiểm tra lại đường dẫn!")
        return

    # 1. Định nghĩa 3 độ phân giải
    resolutions = {
        'SD': (640, 360),    
        'HD': (1280, 720),   
        'FHD': (1920, 1080)  # Thêm lại Full HD
    }
    
    if not os.path.exists('movie'):
        os.makedirs('movie')

    # 2. Mở file để ghi
    files = {
        'SD': open('movie/movie_SD.Mjpeg', 'wb'),
        'HD': open('movie/movie_HD.Mjpeg', 'wb'),
        'FHD': open('movie/movie_FHD.Mjpeg', 'wb')
    }
    
    target_fps = 20.0
    frame_interval_ms = 1000.0 / target_fps # Chính xác 50ms cho mỗi frame

    exported_frame_count = 0
    next_target_time_ms = 0.0

    print("Bắt đầu cắt nén đồng bộ thời gian thực (Time-based Sync)...")

    while True:
        ret, frame = cap.read()
        if not ret:
            break
            
        # LẤY THỜI GIAN THỰC tế của frame hiện tại trong video (đơn vị mili-giây)
        current_time_ms = cap.get(cv2.CAP_PROP_POS_MSEC)
        
        # Nếu dòng thời gian của video đã chạm hoặc vượt mốc thời gian mục tiêu
        if current_time_ms >= next_target_time_ms:
            exported_frame_count += 1
            # Tăng mốc thời gian mục tiêu lên 50ms cho frame tiếp theo
            next_target_time_ms += frame_interval_ms
            
            for quality, res in resolutions.items():
                resized_frame = cv2.resize(frame, res)
                
                # Ép chất lượng JPEG 80% để tối ưu dung lượng đường truyền
                encode_param = [int(cv2.IMWRITE_JPEG_QUALITY), 80]
                success, buffer = cv2.imencode('.jpg', resized_frame, encode_param)
                
                if success:
                    img_bytes = buffer.tobytes()
                    length = len(img_bytes)
                    header = str(length).zfill(5).encode('utf-8')
                    
                    files[quality].write(header)
                    files[quality].write(img_bytes)
        
        if exported_frame_count % 50 == 0 and exported_frame_count > 0:
            # In tiến độ để theo dõi, mỗi 50 frames tương đương 2.5 giây video
            print(f"Đã đóng gói {exported_frame_count} frames (Khoảng {exported_frame_count/20:.1f} giây video)...")

    cap.release()
    for f in files.values():
        f.close()
        
    print(f"\n🎉 HOÀN TẤT! Đã tạo thành công {exported_frame_count} frames cho SD, HD và FHD.")

if __name__ == "__main__":
    # Thay tên file ở đây
    create_multi_quality_mjpeg('video_goc.mp4')
