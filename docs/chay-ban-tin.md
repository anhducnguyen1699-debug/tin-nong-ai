# Quy trình chạy bản tin mỗi sáng (Claude làm phần AI, không gọi API)

Tài liệu này dành cho phiên Claude chạy tự động lúc 6h12 sáng (giờ Việt Nam). Làm đúng thứ tự, không hỏi lại người dùng. Bước nào bị chặn (mạng, quyền, lỗi) thì ghi lại để báo cáo ở bước 6 rồi làm tiếp các bước còn lại nếu được.

Trang đọc tin: https://claude.ai/artifact/M9tBfkKnfHdPBSLd53T91E

## 0. Chuẩn bị (khoảng 1 phút)
Kho `anhducnguyen1699-debug/tin-nong-ai` được gắn sẵn vào lịch chạy (nguồn của routine), thường nằm ở `/home/user/tin-nong-ai`. Không cần `add_repo`. Mã chạy được trên Python 3.11 trở lên.
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

## 3b. Viết bài phân tích sâu cho tin từ 7 điểm
Đọc kỹ `pipeline/prompts/deep.md` trước. Sau đó:
```bash
.venv/bin/python -m pipeline.deep list
```
Làm lần lượt từng sự kiện, điểm cao trước:
1. Đọc toàn văn từng bài báo bằng WebFetch. Đường dẫn `news.google.com` thì dùng WebSearch tìm bài gốc theo tiêu đề cộng tên báo.
2. Tìm thêm 1–3 nguồn gốc bằng WebSearch: thông cáo, văn bản, nghiên cứu, số liệu chính thức.
3. Viết bài theo khuôn, rồi ghi:
   ```bash
   .venv/bin/python -m pipeline.deep write <mã> <<'EOF'
   ...bài...
   EOF
   ```
   Lệnh báo `CHƯA LƯU` thì sửa theo lỗi rồi ghi lại.

Mạng chỉ mở cho một số trang. Ngoài các trang RSS, hiện đọc được toàn văn ở: `www.reuters.com`, `apnews.com`, `www.axios.com`, `www.npr.org`, `www.federalreserve.gov`, `www.newyorkfed.org`, `www.bls.gov`, `www.bea.gov`, `ustr.gov`, `www.fda.gov`, `pubmed.ncbi.nlm.nih.gov`, `www.nejm.org`, `jamanetwork.com`, `www.nature.com`, `tuoitre.vn`, `thanhnien.vn`, `vietnamnet.vn`, `baochinhphu.vn`, `www.sbv.gov.vn`, `moit.gov.vn`.
- Trang bị chặn: tìm bài khác đưa cùng tin trên các trang mở được.
- Sự kiện nào thử hết mà vẫn chưa đủ 3 nguồn đọc toàn văn thì **bỏ qua**, không viết từ tóm tắt tìm kiếm, và ghi tên sự kiện đó vào báo cáo.

Làm xong mọi sự kiện thì xuất lại trang:
```bash
.venv/bin/python -m pipeline.deep render
```

## 4. Đăng trang
Trước hết dùng `Artifact` với `action: "read"` và `url` của trang đọc tin (bắt buộc trước khi đăng vào trang có sẵn). Sau đó dùng `Artifact` để đăng: `file_path: "/home/user/tin-nong-ai/out/preview.html"`, `url` của trang đọc tin. **Không** truyền `capabilities` (để giữ kho phản hồi) và không truyền `icon`.

## 5. Lưu dữ liệu
```bash
git add data/tin.db tests/golden.json
git commit -m "Bản tin ngày $(TZ=Asia/Ho_Chi_Minh date +%d/%m)"
git pull --rebase origin main   # phòng khi trong ngày có thay đổi mã được đẩy lên
git push origin main
```

## 6. Báo cáo
Viết báo cáo bằng tiếng Việt, dưới 120 từ, gồm:
- số bài thu thập, số bài đã chấm, số sự kiện, số thẻ;
- số bài phân tích đã viết, tổng số nguồn đã đọc toàn văn, các sự kiện bị bỏ qua vì không đọc được nguồn;
- 3 tin lớn nhất;
- các lỗi nếu có.
