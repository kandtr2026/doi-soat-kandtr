import streamlit as st
import pandas as pd
import openpyxl
from openpyxl import Workbook
from io import BytesIO
from datetime import datetime
import re

st.set_page_config(page_title="Bank File Merger", page_icon="🏦", layout="wide")

# ── BANK PROFILES ──────────────────────────────────────────────
def detect_bank(rows):
    flat = ' '.join([str(c) for r in rows[:15] for c in r if c])
    if 'BẢNG SAO KÊ GIAO DỊCH' in flat:
        return 'ACB'
    if 'SAO KÊ TÀI KHOẢN' in flat or 'STATEMENT OF ACCOUNT' in flat:
        return 'VCB'
    if 'so but toan' in flat.lower() and 'ngay giao dich' in flat.lower():
        return 'TCB'
    if 'VIETINBANK' in flat.upper() or 'eFAST' in flat:
        return 'VTB'
    if 'MB BANK' in flat.upper() or 'MILITARY' in flat.upper():
        return 'MB'
    return None

def get_account_no(rows, bank_id):
    flat_rows = [' '.join([str(c) for c in r if c]) for r in rows[:15]]
    if bank_id == 'ACB':
        for line in flat_rows:
            m = re.search(r'[Ss]ố tài khoản.*?:\s*(\d+)', line)
            if m: return m.group(1)
            m = re.search(r'[Tt]ài khoản số:\s*(\d+)', line)
            if m: return m.group(1)
    elif bank_id == 'VCB':
        for r in rows[:10]:
            for i, cell in enumerate(r):
                if cell and ('tài khoản' in str(cell).lower() or 'account number' in str(cell).lower()):
                    for j in range(i+1, len(r)):
                        v = str(r[j] or '').strip()
                        if re.match(r'^\d{8,}$', v): return v
                    m = re.search(r'\d{8,}', str(cell))
                    if m: return m.group(0)
    elif bank_id == 'TCB':
        if len(rows) > 1 and len(rows[1]) > 1:
            return str(rows[1][1] or '').strip()
    elif bank_id in ('VTB', 'MB'):
        for r in rows[:15]:
            for i, cell in enumerate(r):
                if cell and ('account no' in str(cell).lower() or 'số tài khoản' in str(cell).lower()):
                    if i+1 < len(r):
                        m = re.search(r'\d{8,}', str(r[i+1] or ''))
                        if m: return m.group(0)
    return 'unknown'

def find_header_row(rows, bank_id):
    kws = {
        'ACB': ['ngày hiệu lực', 'số gd'],
        'VCB': ['debit', 'credit'],
        'TCB': ['so but toan', 'no/debit'],
        'VTB': ['ngày hạch toán', 'nợ/ debit'],
        'MB':  ['ngày giao dịch', 'số tiền'],
    }
    keywords = kws.get(bank_id, [])
    for i, row in enumerate(rows):
        flat = ' '.join([str(c or '').lower() for c in row])
        if all(kw in flat for kw in keywords):
            return i
    return -1

def parse_amount(val):
    """Normalize số: xóa dấu . và , phân cách nghìn → số nguyên"""
    if val is None or str(val).strip() == '': return 0
    s = str(val).strip()
    # Xóa tất cả dấu chấm và phẩy (VN dùng . hoặc , để phân cách nghìn)
    s = re.sub(r'[,\.]', '', s)
    try:
        return int(float(s))
    except:
        return 0

def parse_date(val):
    """Parse date từ nhiều format"""
    if not val: return None
    s = str(val).strip().split('\n')[0]  # VCB merged cell
    patterns = [
        r'^(\d{1,2})/(\d{1,2})/(\d{4})',      # dd/mm/yyyy
        r'^(\d{4})-(\d{2})-(\d{2})',            # yyyy-mm-dd
        r'^(\d{1,2})-(\d{1,2})-(\d{4})',        # dd-mm-yyyy
    ]
    for p in patterns:
        m = re.match(p, s)
        if m:
            g = m.groups()
            try:
                if len(g[0]) == 4:  # yyyy-mm-dd
                    return datetime(int(g[0]), int(g[1]), int(g[2]))
                else:
                    return datetime(int(g[2]), int(g[1]), int(g[0]))
            except:
                continue
    return None

def get_dedup_key(row, headers, bank_id, account_no):
    """Tạo key để dedup"""
    h = [str(h or '').lower() for h in headers]

    # Tìm Số GD / reference
    ref = ''
    for kw in ['số gd', 'so but toan', 'số giao dịch', 'reference', 'số tham chiếu']:
        for i, hh in enumerate(h):
            if kw in hh and i < len(row):
                ref = str(row[i] or '').strip()
                break
        if ref: break

    # Tìm ngày
    date_str = ''
    for kw in ['ngày giao dịch', 'ngay giao dich', 'ngày hạch toán', 'transaction date']:
        for i, hh in enumerate(h):
            if kw in hh and i < len(row):
                date_str = str(row[i] or '').strip()
                break
        if date_str: break
    if not date_str and len(row) > 0:
        date_str = str(row[0] or '').strip()

    # Tìm số tiền
    amounts = []
    for kw in ['tiền', 'debit', 'credit', 'nợ', 'có', 'rút', 'gửi', 'no/', 'co/']:
        for i, hh in enumerate(h):
            if kw in hh and i < len(row):
                v = parse_amount(row[i])
                if v > 0: amounts.append(str(v))

    if ref:
        return f"{bank_id}_{account_no}_{ref}"
    else:
        return f"{bank_id}_{account_no}_{date_str}_{'|'.join(amounts)}"

def normalize_row(row, headers):
    """Normalize số trong row"""
    result = []
    h = [str(h or '').lower() for h in headers]
    for i, cell in enumerate(row):
        col_name = h[i] if i < len(h) else ''
        is_amount = any(kw in col_name for kw in [
            'tiền', 'nợ', 'có', 'debit', 'credit', 'dư', 'balance',
            'rút', 'gửi', 'no/', 'co/', 'amount'
        ])
        if is_amount:
            result.append(parse_amount(cell))
        else:
            result.append(str(cell) if cell is not None else '')
    return result

def read_file(uploaded_file):
    """Đọc file xlsx/xls/csv → list of rows"""
    name = uploaded_file.name.lower()
    if name.endswith('.csv'):
        # Auto-detect separator: thử ; trước rồi ,
        raw = uploaded_file.read()
        # Detect encoding (handle BOM)
        for enc in ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']:
            try:
                text = raw.decode(enc)
                break
            except:
                continue
        # Parse thủ công từng dòng để tránh lỗi pandas với file có số cột không đều
        lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
        rows = []
        for line in lines:
            if not line.strip():
                rows.append([])
                continue
            # Detect separator từ dòng có nhiều field nhất
            sep = ';' if line.count(';') > line.count(',') else ','
            # Parse thủ công handle quoted fields
            cols = []
            cur = ''
            in_q = False
            for ch in line:
                if ch == '"':
                    in_q = not in_q
                elif ch == sep and not in_q:
                    cols.append(cur.strip())
                    cur = ''
                else:
                    cur += ch
            cols.append(cur.strip())
            rows.append(cols)
        return rows
    elif name.endswith('.xls'):
        # Format cũ Excel 97-2003 → dùng xlrd
        import xlrd
        wb = xlrd.open_workbook(file_contents=uploaded_file.read())
        ws = wb.sheet_by_index(0)
        rows = []
        for i in range(ws.nrows):
            rows.append([ws.cell_value(i, j) for j in range(ws.ncols)])
        return rows
    else:
        wb = openpyxl.load_workbook(uploaded_file, data_only=True)
        ws = wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append(list(row))
        return rows

def process_files(files_by_group):
    """Merge + dedup files theo nhóm"""
    results = {}
    for key, info in files_by_group.items():
        bank_id = info['bank_id']
        account_no = info['account_no']
        all_rows_data = info['files']  # list of (rows, filename)

        if not all_rows_data:
            continue

        # Lấy header từ file đầu tiên
        first_rows = all_rows_data[0][0]
        h_idx = find_header_row(first_rows, bank_id)
        if h_idx < 0:
            results[key] = {'error': f'Không tìm thấy header row trong file {all_rows_data[0][1]}'}
            continue

        meta_rows = first_rows[:h_idx]
        header_row = first_rows[h_idx]
        headers = header_row

        # Gom tất cả data rows
        seen = set()
        all_data = []
        total_input = 0
        dup_count = 0

        for rows, fname in all_rows_data:
            this_h = find_header_row(rows, bank_id)
            if this_h < 0: continue

            for row in rows[this_h+1:]:
                # Bỏ qua row rỗng
                flat = ''.join([str(c or '') for c in row]).strip()
                if not flat: continue

                # Check có ngày hợp lệ không
                date_val = row[0] if row else None
                d = parse_date(date_val)
                if not d: continue

                total_input += 1

                # Dedup
                dk = get_dedup_key(row, headers, bank_id, account_no)
                if dk in seen:
                    dup_count += 1
                    continue
                seen.add(dk)

                # Normalize
                clean_row = normalize_row(row, headers)
                all_data.append((d, clean_row))

        # Sort theo ngày tăng dần
        all_data.sort(key=lambda x: x[0])

        if not all_data:
            results[key] = {'error': 'Không có data sau khi lọc'}
            continue

        # Date range cho tên file
        min_date = all_data[0][0]
        max_date = all_data[-1][0]
        fname = f"{bank_id}_{account_no}_{min_date.strftime('%d%m%Y')}to{max_date.strftime('%d%m%Y')}.xlsx"

        # Build output workbook
        wb_out = Workbook()
        ws_out = wb_out.active
        for r in meta_rows:
            ws_out.append([c if c is not None else '' for c in r])
        ws_out.append([c if c is not None else '' for c in header_row])
        for _, row in all_data:
            ws_out.append(row)

        buf = BytesIO()
        wb_out.save(buf)
        buf.seek(0)

        results[key] = {
            'filename': fname,
            'data': buf,
            'tx_count': len(all_data),
            'dup_removed': dup_count,
            'total_input': total_input,
            'date_from': min_date.strftime('%d/%m/%Y'),
            'date_to': max_date.strftime('%d/%m/%Y'),
        }

    return results

# ── UI ─────────────────────────────────────────────────────────
st.title("🏦 Bank File Merger")
st.caption("Upload file sao kê ngân hàng → Tự nhận dạng → Merge + Dedup → Xuất file sạch")

uploaded = st.file_uploader(
    "📂 Upload file sao kê (xlsx, xls, csv) — chọn nhiều file cùng lúc",
    type=['xlsx', 'xls', 'csv'],
    accept_multiple_files=True
)

if uploaded:
    st.divider()

    # Phân nhóm file theo ngân hàng + số TK
    groups = {}
    errors = []

    with st.spinner("🔍 Đang nhận dạng file..."):
        for f in uploaded:
            try:
                rows = read_file(f)
                bank_id = detect_bank(rows)
                if not bank_id:
                    errors.append(f"❓ **{f.name}** — Không nhận dạng được ngân hàng")
                    continue
                account_no = get_account_no(rows, bank_id)
                key = f"{bank_id}_{account_no}"
                if key not in groups:
                    groups[key] = {'bank_id': bank_id, 'account_no': account_no, 'files': []}
                groups[key]['files'].append((rows, f.name))
            except Exception as e:
                errors.append(f"❌ **{f.name}** — Lỗi: {str(e)}")

    # Hiển thị lỗi
    if errors:
        with st.expander("⚠️ File không nhận dạng được", expanded=True):
            for e in errors:
                st.markdown(e)

    if not groups:
        st.warning("Không có file nào được nhận dạng.")
        st.stop()

    # Hiển thị nhóm
    st.subheader(f"📊 Tìm thấy {len(groups)} nhóm từ {len(uploaded)} file")

    cols = st.columns(min(len(groups), 4))
    for i, (key, info) in enumerate(groups.items()):
        with cols[i % len(cols)]:
            st.metric(
                label=f"{info['bank_id']} · {info['account_no']}",
                value=f"{len(info['files'])} file",
            )

    st.divider()

    # Nút merge
    if st.button("⚡ Merge & Dedup tất cả", type="primary", use_container_width=True):
        with st.spinner("⏳ Đang xử lý..."):
            results = process_files(groups)

        st.success(f"✅ Hoàn tất! {len(results)} file đã được tạo")
        st.divider()

        for key, res in results.items():
            if 'error' in res:
                st.error(f"❌ **{key}**: {res['error']}")
                continue

            col1, col2 = st.columns([3, 1])
            with col1:
                st.markdown(f"**📄 {res['filename']}**")
                st.caption(
                    f"✅ {res['tx_count']} giao dịch | "
                    f"🗑️ Bỏ {res['dup_removed']} trùng | "
                    f"📅 {res['date_from']} → {res['date_to']}"
                )
            with col2:
                st.download_button(
                    label="⬇️ Tải về",
                    data=res['data'],
                    file_name=res['filename'],
                    mime='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    key=f"dl_{key}"
                )