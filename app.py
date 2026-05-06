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

# ==========================================
# 1. UI UPLOAD FILE TERPISAH (ANTI-TERTUKAR)
# ==========================================
col1, col2 = st.columns(2)

with col1:
    st.info("📂 **LANGKAH 1:** Upload File Excel Buku Tarif RS (Source)")
    file_rs = st.file_uploader("Pilih File RS", type=['xlsx', 'xls'], key="rs")

with col2:
    st.success("📂 **LANGKAH 2:** Upload File Excel Hasil Migrasi (Target)")
    file_sistem = st.file_uploader("Pilih File Sistem", type=['xlsx', 'xls'], key="sistem")

st.divider()

# ==========================================
# 2. LOGIKA PROSES PANDAS (Dijalankan saat tombol diklik)
# ==========================================
if file_rs and file_sistem:
    if st.button("🚀 Jalankan Audit Validasi Data", use_container_width=True):
        
        with st.spinner("Membaca dan memproses jutaan sel data... Mohon tunggu..."):
            try:
                # Membaca data
                df_source = pd.read_excel(file_rs, sheet_name='Multi Price')
                df_target = pd.read_excel(file_sistem, sheet_name='Multi Price')
                
                # --- PREPROCESSING & NORMALISASI ---
                keys = ['Name', 'Nama Komponen', 'Kewarganegaraan', 'Unit Perawatan', 'Pembayaran', 'Kelas Perawatan']
                cols_to_fill = ['Kewarganegaraan', 'Unit Perawatan', 'Pembayaran', 'Kelas Perawatan']
                
                for df in [df_source, df_target]:
                    for col in cols_to_fill:
                        if col in df.columns:
                            df[col] = df[col].fillna('Semua')

                # --- AUDIT PENJUMLAHAN KOMPONEN (ROLL-UP) ---
                groupby_keys = ['Name', 'Kewarganegaraan', 'Unit Perawatan', 'Pembayaran', 'Kelas Perawatan']
                
                def validate_rollup(df_data, suffix):
                    rollup = df_data.groupby(groupby_keys).agg(
                        Total_Komp=('Harga Komponen', lambda x: pd.to_numeric(x, errors='coerce').sum()),
                        Harga_Jual_Base=('Harga Jual', 'max'),
                        Has_Duplicate_Komp=('Nama Komponen', lambda x: x.duplicated().any())
                    ).reset_index()
                    
                    # Kondisi 1: Komponen 0, Harga Jual Ada
                    rollup[f'No_Komp_{suffix}'] = (rollup['Total_Komp'] == 0) & (rollup['Harga_Jual_Base'] > 0)
                    # Kondisi 2: Duplikasi Komponen
                    rollup[f'Dup_Komp_{suffix}'] = rollup['Has_Duplicate_Komp']
                    # Kondisi 3: Total Komponen != Harga Jual
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

                    # Cek Reference Type 4
                    if not is_missing_rs and row.get('Reference Type ID_RS') == 4:
                        if row.get('Harga Beli_RS') != row.get('Harga Jual_RS'):
                            errors.append("⚠️ (RS) Ref 4: Beli ≠ Jual")
                    if not is_missing_sistem and row.get('Reference Type ID_Sistem') == 4:
                        if row.get('Harga Beli_Sistem') != row.get('Harga Jual_Sistem'):
                            errors.append("⚠️ (Sistem) Ref 4: Beli ≠ Jual")

                    # Cek Rollup RS
                    if not is_missing_rs:
                        if row.get('No_Komp_RS'):
                            errors.append("⚠️ (RS) Tindakan tidak memiliki tarif komponen")
                        elif row.get('Rollup_Error_RS'):
                            if row.get('Dup_Komp_RS'):
                                errors.append("⚠️ (RS) Total Komponen ≠ Harga Jual (Kemungkinan komponen duplicate)")
                            else:
                                errors.append("⚠️ (RS) Total Komponen ≠ Harga Jual")

                    # Cek Rollup Sistem
                    if not is_missing_sistem:
                        if row.get('No_Komp_Sistem'):
                            errors.append("⚠️ (Sistem) Tindakan tidak memiliki tarif komponen")
                        elif row.get('Rollup_Error_Sistem'):
                            if row.get('Dup_Komp_Sistem'):
                                errors.append("⚠️ (Sistem) Total Komponen ≠ Harga Jual (Kemungkinan komponen duplicate)")
                            else:
                                errors.append("⚠️ (Sistem) Total Komponen ≠ Harga Jual")

                    return " | ".join(errors)

                df_merge['Keterangan Error'] = df_merge.apply(check_errors, axis=1)

                # Filter khusus error
                df_exception = df_merge[df_merge['Keterangan Error'] != ""].copy()

                # --- FORMATTING OUTPUT ---
                df_exception['Nama Tindakan'] = df_exception['Name']
                df_exception['Hierarki (Unit-Payer-Kelas)'] = df_exception['Unit Perawatan'].astype(str) + " - " + \
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

                # --- PENERAPAN WARNA (STYLING) ---
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
                        
                    if "Total Komponen ≠ Harga Jual" in err_msg or "tidak memiliki tarif komponen" in err_msg: 
                        if "(RS)" in err_msg: set_color('Harga Jual (RS)', '#e6f2ff')
                        if "(Sistem)" in err_msg: set_color('Harga Jual (Sistem)', '#e6f2ff')

                    return styles

                styled_final = df_final.style.apply(apply_styles, axis=1)

                # ==========================================
                # 3. TAMPILAN HASIL DI HALAMAN WEB
                # ==========================================
                st.success(f"✅ Audit Selesai! Ditemukan **{len(df_final)} baris** yang memerlukan validasi atau perbaikan.")
                
                # Tampilkan tabel preview di layar
                st.dataframe(styled_final, height=400, use_container_width=True)

                # Siapkan file Excel untuk di-download
                output = io.BytesIO()
                styled_final.to_excel(output, index=False, engine='openpyxl')
                output.seek(0)

                st.download_button(
                    label="📥 Download Laporan Audit (Excel)",
                    data=output,
                    file_name="File_1_Validasi_Kualitas_Data_SIMRS.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True
                )

            except Exception as e:
                st.error(f"Terjadi kesalahan saat memproses data. Pastikan format file sudah benar. Detail Error: {e}")
