import streamlit as st
import pandas as pd
import numpy as np
import io

# ==========================================
# PENGATURAN TAMPILAN HALAMAN STREAMLIT
# ==========================================
st.set_page_config(
    page_title="Validasi Data Migrasi SIMRS",
    page_icon="🏥",
    layout="wide"
)

st.title("🏥 Portal Validasi Data Migrasi SIMRS")
st.markdown("""
Aplikasi ini digunakan untuk membandingkan **Buku Tarif Master (RS)** dengan **Hasil Tarikan Sistem (Migrasi)**. 
Sistem akan melakukan audit otomatis terhadap selisih harga, anomali komponen, dan aturan logika *billing* SIMRS.
""")
st.divider()

# Konstanta Batasan File
MAX_FILE_SIZE_MB = 5
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
ALLOWED_TYPES = ['xlsx', 'xls', 'csv', 'xlsm', 'xlsb']

# ==========================================
# 1. UI UPLOAD FILE TERPISAH
# ==========================================
col1, col2 = st.columns(2)

with col1:
    st.info(f"📂 **LANGKAH 1:** Upload File Buku Tarif RS (Maks. {MAX_FILE_SIZE_MB}MB)")
    file_rs = st.file_uploader("Pilih File RS", type=ALLOWED_TYPES, key="rs")

with col2:
    st.success(f"📂 **LANGKAH 2:** Upload File Hasil Migrasi Sistem (Maks. {MAX_FILE_SIZE_MB}MB)")
    file_sistem = st.file_uploader("Pilih File Sistem", type=ALLOWED_TYPES, key="sistem")

st.divider()

# Fungsi Membaca File
def load_data(uploaded_file):
    file_name = uploaded_file.name.lower()
    if file_name.endswith('.csv'):
        try:
            return pd.read_csv(uploaded_file)
        except:
            uploaded_file.seek(0)
            return pd.read_csv(uploaded_file, sep=';')
    else:
        return pd.read_excel(uploaded_file, sheet_name='Multi Price')

# ==========================================
# 2. LOGIKA PROSES PANDAS
# ==========================================
if file_rs and file_sistem:
    if file_rs.size > MAX_FILE_SIZE_BYTES:
        st.error(f"❌ File **{file_rs.name}** terlalu besar! ({file_rs.size / (1024*1024):.2f} MB). Batas maksimal adalah {MAX_FILE_SIZE_MB} MB.")
    elif file_sistem.size > MAX_FILE_SIZE_BYTES:
        st.error(f"❌ File **{file_sistem.name}** terlalu besar! ({file_sistem.size / (1024*1024):.2f} MB). Batas maksimal adalah {MAX_FILE_SIZE_MB} MB.")
    else:
        if st.button("🚀 Jalankan Audit Validasi Data", use_container_width=True):
            
            with st.spinner("Membaca dan memproses data... Mohon tunggu..."):
                try:
                    df_source = load_data(file_rs)
                    df_target = load_data(file_sistem)
                    
                    # --- PREPROCESSING & NORMALISASI ---
                    keys = ['Name', 'Nama Komponen', 'Kewarganegaraan', 'Tipe Kunjungan', 'Pembayaran', 'Kelas Perawatan']
                    cols_to_fill = ['Kewarganegaraan', 'Tipe Kunjungan', 'Pembayaran', 'Kelas Perawatan']
                    
                    for df in [df_source, df_target]:
                        missing_cols = [col for col in keys if col not in df.columns]
                        if missing_cols:
                            st.error(f"File tidak valid. Kolom berikut tidak ditemukan di data Anda: {', '.join(missing_cols)}")
                            st.stop()
                            
                        for col in cols_to_fill:
                            if col in df.columns:
                                df[col] = df[col].fillna('Semua')

                    # --- AUDIT PENJUMLAHAN KOMPONEN (ROLL-UP) ---
                    groupby_keys = ['Name', 'Kewarganegaraan', 'Tipe Kunjungan', 'Pembayaran', 'Kelas Perawatan']
                    
                    def validate_rollup(df_data, suffix):
                        rollup = df_data.groupby(groupby_keys).agg(
                            Total_Komp=('Harga Komponen', lambda x: pd.to_numeric(x, errors='coerce').sum()),
                            Harga_Jual_Base=('Harga Jual', 'max'),
                            Has_Duplicate_Komp=('Nama Komponen', lambda x: x.duplicated().any())
                        ).reset_index()
                        
                        rollup[f'No_Komp_{suffix}'] = (rollup['Total_Komp'] == 0) & (rollup['Harga_Jual_Base'] > 0)
                        rollup[f'Dup_Komp_{suffix}'] = rollup['Has_Duplicate_Komp']
                        rollup[f'Rollup_Error_{suffix}'] = np.where(pd.notna(rollup['Harga_Jual_Base']), 
                                                     rollup['Total_Komp'] != rollup['Harga_Jual_Base'], False)
                                                     
                        cols_to_merge = groupby_keys + [f'Rollup_Error_{suffix}', f'No_Komp_{suffix}', f'Dup_Komp_{suffix}']
                        return df_data.merge(rollup[cols_to_merge], on=groupby_keys, how='left')

                    df_source = validate_rollup(df_source, 'RS')
                    df_target = validate_rollup(df_target, 'Sistem')

                    # --- MERGE DATA ---
                    df_merge = pd.merge(df_source, df_target, on=keys, suffixes=('_RS', '_Sistem'), how='outer')

                    # --- BUSINESS RULE ENGINE ---
                    def check_errors(row):
                        errors = []
                        is_missing_rs = pd.isna(row.get('Harga Jual_RS'))
                        is_missing_sistem = pd.isna(row.get('Harga Jual_Sistem'))

                        if is_missing_rs: errors.append("⚠️ Komponen Muncul Tiba-tiba di Sistem (Orphan)")
                        elif is_missing_sistem: errors.append("⚠️ Gagal Migrasi (Komponen Hilang di Sistem)")
                        else:
                            if str(row.get('Kode Akun_RS', '')) != str(row.get('Kode Akun_Sistem', '')):
                                errors.append("❌ Kode Akun Beda")
                            if row.get('Harga Beli_RS') != row.get('Harga Beli_Sistem'):
                                errors.append("❌ Selisih Harga Beli (Migrasi)")
                            if row.get('Harga Jual_RS') != row.get('Harga Jual_Sistem'):
                                errors.append("❌ Selisih Harga Jual (Migrasi)")

                        def cek_aturan_margin(suffix):
                            ref_id = row.get(f'Reference Type ID_{suffix}')
                            if pd.notna(ref_id) and ref_id in [1, 7]:
                                h_beli = row.get(f'Harga Beli_{suffix}')
                                h_jual = row.get(f'Harga Jual_{suffix}')
                                
                                if pd.isna(h_beli) or pd.isna(h_jual):
                                    return None
                                    
                                m_persen = row.get(f'Margin Persen_{suffix}')
                                m_total = row.get(f'Margin Total_{suffix}')
                                calc_jual = None
                                
                                if pd.notna(m_persen) and str(m_persen).strip() != '':
                                    try:
                                        val_str = str(m_persen).strip()
                                        if '%' in val_str:
                                            p_val = float(val_str.replace('%', '')) / 100.0
                                        else:
                                            p_val = float(m_persen)
                                            if p_val > 1: p_val = p_val / 100.0
                                        calc_jual = h_beli + (h_beli * p_val)
                                    except:
                                        pass
                                
                                if calc_jual is None and pd.notna(m_total) and str(m_total).strip() != '':
                                    try:
                                        calc_jual = h_beli + float(m_total)
                                    except:
                                        pass
                                        
                                if calc_jual is not None:
                                    calc_jual_round = round(calc_jual, 2)
                                    h_jual_round = round(float(h_jual), 2)
                                    
                                    if calc_jual_round != h_jual_round:
                                        return f"❌ ({suffix}) Ref {int(ref_id)}: Harga Jual ≠ Perhitungan Margin (Ekspektasi: {calc_jual_round})"
                            return None

                        if not is_missing_rs and row.get('Reference Type ID_RS') == 4:
                            if row.get('Harga Beli_RS') != row.get('Harga Jual_RS'):
                                errors.append("⚠️ (RS) Ref 4: Beli ≠ Jual")
                        if not is_missing_sistem and row.get('Reference Type ID_Sistem') == 4:
                            if row.get('Harga Beli_Sistem') != row.get('Harga Jual_Sistem'):
                                errors.append("⚠️ (Sistem) Ref 4: Beli ≠ Jual")

                        if not is_missing_rs:
                            err_margin_rs = cek_aturan_margin('RS')
                            if err_margin_rs: errors.append(err_margin_rs)
                            
                            if row.get('No_Komp_RS'):
                                errors.append("⚠️ (RS) Tindakan tidak memiliki tarif komponen")
                            elif row.get('Rollup_Error_RS'):
                                if row.get('Dup_Komp_RS'):
                                    errors.append("⚠️ (RS) Total Komponen ≠ Harga Jual (Kemungkinan komponen duplicate)")
                                else:
                                    errors.append("⚠️ (RS) Total Komponen ≠ Harga Jual")

                        if not is_missing_sistem:
                            err_margin_sys = cek_aturan_margin('Sistem')
                            if err_margin_sys: errors.append(err_margin_sys)
                            
                            if row.get('No_Komp_Sistem'):
                                errors.append("⚠️ (Sistem) Tindakan tidak memiliki tarif komponen")
                            elif row.get('Rollup_Error_Sistem'):
                                if row.get('Dup_Komp_Sistem'):
                                    errors.append("⚠️ (Sistem) Total Komponen ≠ Harga Jual (Kemungkinan komponen duplicate)")
                                else:
                                    errors.append("⚠️ (Sistem) Total Komponen ≠ Harga Jual")

                        return "\n".join(errors)

                    df_merge['Keterangan Error'] = df_merge.apply(check_errors, axis=1)
                    df_exception = df_merge[df_merge['Keterangan Error'] != ""].copy()

                    # --- FORMATTING OUTPUT ---
                    df_exception['Nama Tindakan'] = df_exception['Name']
                    df_exception['Hierarki (Unit-Payer-Kelas)'] = df_exception['Tipe Kunjungan'].astype(str) + " - " + \
                                                                  df_exception['Pembayaran'].astype(str) + " - " + \
                                                                  df_exception['Kelas Perawatan'].astype(str)
                    
                    df_exception['Nama Komp. (RS)'] = np.where(pd.notna(df_exception['Harga Jual_RS']), df_exception['Nama Komponen'], "❌ (Tidak Ada)")
                    df_exception['Nama Komp. (Sistem)'] = np.where(pd.notna(df_exception['Harga Jual_Sistem']), df_exception['Nama Komponen'], "❌ (Tidak Ada)")
                    
                    df_exception['Kode Akun (RS)'] = df_exception['Kode Akun_RS']
                    df_exception['Kode Akun (Sistem)'] = df_exception['Kode Akun_Sistem']
                    df_exception['Harga Beli (RS)'] = df_exception['Harga Beli_RS']
                    df_exception['Harga Beli (Sistem)'] = df_exception['Harga Beli_Sistem']
                    df_exception['Harga Jual (RS)'] = df_exception['Harga Jual_RS']
                    df_exception['Harga Jual (Sistem)'] = df_exception['Harga Jual_Sistem']

                    cols_final = [
                        'Nama Tindakan', 'Hierarki (Unit-Payer-Kelas)', 'Keterangan Error',
                        'Nama Komp. (RS)', 'Nama Komp. (Sistem)',
                        'Kode Akun (RS)', 'Kode Akun (Sistem)',
                        'Harga Beli (RS)', 'Harga Beli (Sistem)',
                        'Harga Jual (RS)', 'Harga Jual (Sistem)'
                    ]
                    df_final = df_exception[cols_final]

                    # --- PENERAPAN WARNA (STYLING) UTAMA ---
                    def apply_styles(row):
                        styles = [''] * len(row)
                        err_msg = str(row['Keterangan Error'])
                        
                        def set_color(col_name, color):
                            if col_name in row.index:
                                styles[row.index.get_loc(col_name)] = f'background-color: {color}; color: black;'

                        if "Gagal Migrasi" in err_msg: set_color('Nama Komp. (Sistem)', '#ffcccc')
                        if "Orphan" in err_msg: set_color('Nama Komp. (RS)', '#ffcccc')
                        
                        if "Kode Akun Beda" in err_msg: set_color('Kode Akun (Sistem)', '#ffff99')
                        if "Selisih Harga Beli" in err_msg: set_color('Harga Beli (Sistem)', '#ffff99')
                        if "Selisih Harga Jual" in err_msg: set_color('Harga Jual (Sistem)', '#ffff99')
                        
                        if "(RS) Ref 4" in err_msg: 
                            set_color('Harga Beli (RS)', '#ffe6cc')
                            set_color('Harga Jual (RS)', '#ffe6cc')
                        if "(Sistem) Ref 4" in err_msg: 
                            set_color('Harga Beli (Sistem)', '#ffe6cc')
                            set_color('Harga Jual (Sistem)', '#ffe6cc')
                            
                        if "Perhitungan Margin" in err_msg:
                            if "(RS)" in err_msg:
                                set_color('Harga Beli (RS)', '#e6ccff')
                                set_color('Harga Jual (RS)', '#e6ccff')
                            if "(Sistem)" in err_msg:
                                set_color('Harga Beli (Sistem)', '#e6ccff')
                                set_color('Harga Jual (Sistem)', '#e6ccff')
                            
                        if "Total Komponen ≠ Harga Jual" in err_msg or "tidak memiliki tarif komponen" in err_msg: 
                            if "(RS)" in err_msg: set_color('Harga Jual (RS)', '#e6f2ff')
                            if "(Sistem)" in err_msg: set_color('Harga Jual (Sistem)', '#e6f2ff')

                        return styles

                    styled_final = df_final.style.apply(apply_styles, axis=1)
                    styled_final = styled_final.set_properties(subset=['Keterangan Error'], **{'white-space': 'pre-wrap'})

                    # ==========================================
                    # 3. PENYUSUNAN SHEET LEGENDA WARNA
                    # ==========================================
                    legend_data = {
                        "Warna": ["Merah Muda", "Kuning", "Oranye Muda", "Ungu Muda", "Biru Muda"],
                        "Kode Hex": ["#ffcccc", "#ffff99", "#ffe6cc", "#e6ccff", "#e6f2ff"],
                        "Penjelasan / Arti Error": [
                            "Gagal Migrasi (Komponen Hilang di Sistem) / Orphan (Komponen Muncul Tiba-tiba di Sistem)",
                            "Perbedaan Data Migrasi (Kode Akun Beda, Selisih Harga Beli, Selisih Harga Jual)",
                            "Anomali Aturan Harga (Ref Type 4: Harga Beli ≠ Harga Jual)",
                            "Kesalahan Perhitungan Margin Farmasi/Alkes (Ref Type 1 & 7: Harga Jual ≠ Perhitungan Margin)",
                            "Kesalahan Penjumlahan (Total Harga Komponen ≠ Harga Jual) ATAU Tindakan Tidak Memiliki Komponen"
                        ]
                    }
                    df_legend = pd.DataFrame(legend_data)

                    # Fungsi mewarnai sheet legenda sesuai warnanya masing-masing
                    def style_legend(row):
                        hex_code = row['Kode Hex']
                        return [f'background-color: {hex_code}; color: black;'] * 2 + ['']

                    styled_legend = df_legend.style.apply(style_legend, axis=1)

                    # ==========================================
                    # 4. TAMPILAN WEB & MULTI-SHEET EXCEL EXPORT
                    # ==========================================
                    st.success(f"✅ Audit Selesai! Ditemukan **{len(df_final)} baris** yang memerlukan validasi atau perbaikan.")
                    st.dataframe(styled_final, height=400, use_container_width=True)

                    # Menggunakan pd.ExcelWriter untuk membuat banyak sheet
                    output = io.BytesIO()
                    with pd.ExcelWriter(output, engine='openpyxl') as writer:
                        styled_final.to_excel(writer, sheet_name='Hasil Audit Migrasi', index=False)
                        styled_legend.to_excel(writer, sheet_name='Legenda Warna', index=False)
                    
                    output.seek(0)

                    st.download_button(
                        label="📥 Download Laporan Audit (Excel)",
                        data=output,
                        file_name="Laporan_Validasi_Kualitas_Data_SIMRS.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True
                    )

                except Exception as e:
                    st.error(f"Terjadi kesalahan saat memproses data. Pastikan format file sudah benar. Detail Error: {e}")
