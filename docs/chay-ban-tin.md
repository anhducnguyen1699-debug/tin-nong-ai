# Quy trình chạy bản tin mỗi sáng (Claude làm phần AI, không gọi API)

Tài liệu này dành cho phiên Claude chạy tự động lúc 6h12 sáng (giờ Việt Nam). Làm đúng thứ tự, không hỏi lại người dùng. Bước nào bị chặn (mạng, quyền, lỗi) thì ghi lại để báo cáo ở bước 6 rồi làm tiếp các bước còn lại nếu được.

Trang đọc tin: https://claude.ai/artifact/M9tBfkKnfHdPBSLd53T91E

## 0. Chuẩn bị (khoảng 1 phút)
```bash
cd /home/user/tin-nong-ai
python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
```

## 1. Nạp phản hồi của người đọc
Dùng công cụ `ArtifactData`: `action: "list"`, `url` là trang đọc tin ở trên, `collection: "ratings"`, `query: {"limit": 1000}`, `out_dir: "/tmp/phan-hoi"`. Sau đó:
```bash
.venv/bin/python -m pipeline.ratings /tmp/phan-hoi/ratings --db data/tin.db
```
Không có phiếu nào thì bỏ qua bước này. Phiếu "Hữu ích/Bỏ qua" sẽ thành ví dụ sở thích trong đề chấm điểm.

## 2. Đọc luật chấm (một lần, trước khi trả lời đề nào)
Đọc kỹ 4 file sau. Đây là luật bạn phải theo khi đóng vai AI:
`config/profile.md`, `pipeline/prompts/classify.md`, `pipeline/prompts/cluster.md`, `pipeline/prompts/summarize.md`.

## 3. Chạy đường ống
```bash
.venv/bin/python -m pipeline.hand reset
.venv/bin/python -m pipeline.hand next --collect --sample 300
```
Lệnh in ra một đề đang chờ, kèm định dạng câu trả lời gọn ở dòng đầu. Lặp lại vòng sau tới khi lệnh in `XONG`:
1. Đọc đề, trả lời theo luật ở bước 2.
2. Ghi câu trả lời:
   ```bash
   .venv/bin/python -m pipeline.hand answer <<'EOF'
   ...câu trả lời...
   EOF
   ```
3. Chạy tiếp: `.venv/bin/python -m pipeline.hand next`

Thường có khoảng 6 đề chấm điểm (50 bài một đề), 3–6 đề gộp sự kiện, 10–15 đề viết thẻ.

Lưu ý:
- **Chấm điểm:** đủ mọi bài, mỗi bài một dòng. Phần lớn bài chỉ đáng 3–6 điểm. Người đọc hay chê AI chấm thấp tin địa chính trị gần Việt Nam (Biển Đông, thuế quan), tin lãi suất và tin AI y tế.
- **Gộp sự kiện:** chỉ gộp các bài nói cùng một chuyện cụ thể. Dùng `e<mã>` để gắn vào sự kiện đã có trong `<su_kien_dang_mo>`.
- **Viết thẻ:** chỉ dựa vào nguồn được đưa. "Nên làm gì" luôn phải có việc cụ thể (mốc ngày, chỉ số, tài liệu); không bao giờ chỉ viết "chỉ cần nắm thông tin". Không khuyên mua bán mã cụ thể.
- Nếu `answer` báo sai khuôn thì sửa và gửi lại. Không sửa mã nguồn trong lượt chạy hằng ngày.

## 4. Đăng trang
Trước hết dùng `Artifact` với `action: "read"` và `url` của trang đọc tin (bắt buộc trước khi đăng vào trang có sẵn). Sau đó dùng `Artifact` để đăng: `file_path: "/home/user/tin-nong-ai/out/preview.html"`, `url` của trang đọc tin. **Không** truyền `capabilities` (để giữ kho phản hồi) và không truyền `icon`.

## 5. Lưu dữ liệu
```bash
git add data/tin.db tests/golden.json
git commit -m "Bản tin ngày $(TZ=Asia/Ho_Chi_Minh date +%d/%m)"
git push origin main
```

## 6. Báo cáo
Viết báo cáo bằng tiếng Việt, dưới 100 từ, gồm: số bài thu thập, số bài đã chấm, số sự kiện, số thẻ, 3 tin lớn nhất, và các lỗi nếu có.
