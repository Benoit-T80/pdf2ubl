import io
import re
import zipfile
import base64
from datetime import datetime
import streamlit as st
import pdfplumber
from lxml import etree

st.set_page_config(page_title="PDF2UBL - Convertisseur Peppol", layout="wide", page_icon="📄")

# --- VÉRIFICATION DU MOT DE PASSE ---
def check_password():
    if "authenticated" not in st.session_state:
        st.session_state.authenticated = False

    if st.session_state.authenticated:
        return True

    secret_pwd = st.secrets.get("APP_PASSWORD")

    col_login, _ = st.columns([1, 2])
    with col_login:
        st.markdown("### 🔒 Accès restreint")
        pwd_input = st.text_input("Veuillez saisir le mot de passe :", type="password")
        if st.button("Se connecter"):
            if pwd_input == secret_pwd:
                st.session_state.authenticated = True
                st.rerun()
            else:
                st.error("Mot de passe incorrect.")
    return False

if not check_password():
    st.stop()
# -------------------------------------

def clean_vat(vat_str: str) -> str:
    if not vat_str:
        return ""
    cleaned = re.sub(r"[^0-9A-Za-z]", "", vat_str).upper()
    if len(cleaned) == 10 and not cleaned.startswith("BE"):
        cleaned = "BE" + cleaned
    return cleaned

def detect_regime(text: str) -> str:
    """Détecte le régime fiscal et le taux prédominant."""
    t_lower = text.lower()
    if "cocontractant" in t_lower or "medecontractant" in t_lower or "ar n°1" in t_lower:
        return "Cocontractant (0% - Case 87)"
    if "intracommunautaire" in t_lower or "intra-communautaire" in t_lower or "ic" in t_lower:
        return "Intracommunautaire (0% - Case 86)"
    if re.search(r"\b6(\s?%|\.00%|,00%)\b", text):
        return "Taux réduit 6%"
    if re.search(r"\b12(\s?%|\.00%|,00%)\b", text):
        return "Taux intermédiaire 12%"
    if re.search(r"\b0(\s?%|\.00%|,00%)\b", text):
        return "Exonéré / Art. 44 (0%)"
    return "Taux standard 21%"

def parse_pdf_data(file_bytes: bytes) -> dict:
    text = ""
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            t = page.extract_text()
            if t:
                text += t + "\n"

    # Détection TVA / BCE
    vat_match = re.search(r"\b(BE\s?[01]\d{3}[\s.]?\d{3}[\s.]?\d{3})\b", text, re.IGNORECASE)
    supplier_vat = clean_vat(vat_match.group(1)) if vat_match else "BE0000000000"

    # Détection Communication structurée (VCS)
    vcs_match = re.search(r"\+{3}\s?(\d{3})\/(\d{4})\/(\d{5})\s?\+{3}", text)
    if not vcs_match:
        vcs_match = re.search(r"\*{3}\s?(\d{3})\/(\d{4})\/(\d{5})\s?\*{3}", text)
    vcs = f"+++{vcs_match.group(1)}/{vcs_match.group(2)}/{vcs_match.group(3)}+++" if vcs_match else ""

    # Détection Date
    date_match = re.search(r"\b(\d{2})[\/\.-](\d{2})[\/\.-](20\d{2})\b", text)
    if date_match:
        d, m, y = date_match.groups()
        issue_date = f"{y}-{m}-{d}"
    else:
        issue_date = datetime.today().strftime("%Y-%m-%d")

    # Détection Montants (Recherche du plus élevé pour le TTC)
    amounts = re.findall(r"\b\d{1,5}[,\.]\d{2}\b", text)
    parsed_floats = []
    for a in amounts:
        try:
            val = float(a.replace(",", "."))
            parsed_floats.append(val)
        except ValueError:
            pass

    gross_amount = max(parsed_floats) if parsed_floats else 0.0
    regime = detect_regime(text)

    return {
        "invoice_id": "INV-" + datetime.today().strftime("%Y%m%d%H%M"),
        "issue_date": issue_date,
        "due_date": issue_date,
        "supplier_name": "Fournisseur Identifié",
        "supplier_vat": supplier_vat,
        "vcs": vcs,
        "regime": regime,
        "gross_amount": gross_amount,
    }

def generate_ubl_xml(data: dict, pdf_bytes: bytes, filename: str) -> bytes:
    nsmap = {
        None: "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
        "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
        "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    }
    root = etree.Element("Invoice", nsmap=nsmap)

    # Entête Peppol BIS 3.0 / EN 16931
    etree.SubElement(root, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}CustomizationID").text = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
    etree.SubElement(root, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ProfileID").text = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
    etree.SubElement(root, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID").text = str(data["invoice_id"])
    etree.SubElement(root, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}IssueDate").text = str(data["issue_date"])
    etree.SubElement(root, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}DueDate").text = str(data["due_date"])
    etree.SubElement(root, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}InvoiceTypeCode").text = "380"
    etree.SubElement(root, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}DocumentCurrencyCode").text = "EUR"

    # PDF embarqué en Base64 pour l'appariement Virtual Invoice
    add_doc = etree.SubElement(root, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}AdditionalDocumentReference")
    etree.SubElement(add_doc, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID").text = filename
    attach = etree.SubElement(add_doc, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}Attachment")
    bin_obj = etree.SubElement(attach, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}EmbeddedDocumentBinaryObject", mimeCode="application/pdf", filename=filename)
    bin_obj.text = base64.b64encode(pdf_bytes).decode("ascii")

    # Fournisseur
    sup_party = etree.SubElement(root, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}AccountingSupplierParty")
    party = etree.SubElement(sup_party, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}Party")
    pname = etree.SubElement(party, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}PartyName")
    etree.SubElement(pname, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}Name").text = data["supplier_name"]
    ptax = etree.SubElement(party, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}PartyTaxScheme")
    etree.SubElement(ptax, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}CompanyID").text = data["supplier_vat"]
    tscheme = etree.SubElement(ptax, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}TaxScheme")
    etree.SubElement(tscheme, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID").text = "VAT"

    # Communication structurée
    if data["vcs"]:
        pmeans = etree.SubElement(root, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}PaymentMeans")
        etree.SubElement(pmeans, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}PaymentMeansCode").text = "58"
        etree.SubElement(pmeans, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}PaymentID").text = data["vcs"]

    # TaxTotal et Grille TVA WinBooks
    taxtotal = etree.SubElement(root, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}TaxTotal")
    etree.SubElement(taxtotal, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}TaxAmount", currencyID="EUR").text = f"{data['tax_amount']:.2f}"

    tax_subtotal = etree.SubElement(taxtotal, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}TaxSubtotal")
    etree.SubElement(tax_subtotal, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}TaxableAmount", currencyID="EUR").text = f"{data['net_amount']:.2f}"
    etree.SubElement(tax_subtotal, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}TaxAmount", currencyID="EUR").text = f"{data['tax_amount']:.2f}"

    tax_category = etree.SubElement(tax_subtotal, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}TaxCategory")
    etree.SubElement(tax_category, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID").text = data["tax_category_code"]
    etree.SubElement(tax_category, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}Percent").text = f"{data['rate']:.2f}"
    if data.get("exemption_reason"):
        etree.SubElement(tax_category, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}TaxExemptionReason").text = data["exemption_reason"]

    tax_cat_scheme = etree.SubElement(tax_category, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}TaxScheme")
    etree.SubElement(tax_cat_scheme, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID").text = "VAT"

    # LegalMonetaryTotal
    legal = etree.SubElement(root, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}LegalMonetaryTotal")
    etree.SubElement(legal, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}LineExtensionAmount", currencyID="EUR").text = f"{data['net_amount']:.2f}"
    etree.SubElement(legal, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}TaxExclusiveAmount", currencyID="EUR").text = f"{data['net_amount']:.2f}"
    etree.SubElement(legal, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}TaxInclusiveAmount", currencyID="EUR").text = f"{data['gross_amount']:.2f}"
    etree.SubElement(legal, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}PayableAmount", currencyID="EUR").text = f"{data['gross_amount']:.2f}"

    # Ligne de facture détaillée pour mapping automatique WinBooks
    inv_line = etree.SubElement(root, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}InvoiceLine")
    etree.SubElement(inv_line, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID").text = "1"
    etree.SubElement(inv_line, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}InvoicedQuantity", unitCode="C62").text = "1"
    etree.SubElement(inv_line, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}LineExtensionAmount", currencyID="EUR").text = f"{data['net_amount']:.2f}"

    item = etree.SubElement(inv_line, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}Item")
    etree.SubElement(item, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}Name").text = "Prestation / Marchandise"
    item_tax = etree.SubElement(item, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}ClassifiedTaxCategory")
    etree.SubElement(item_tax, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID").text = data["tax_category_code"]
    etree.SubElement(item_tax, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}Percent").text = f"{data['rate']:.2f}"
    item_tax_scheme = etree.SubElement(item_tax, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}TaxScheme")
    etree.SubElement(item_tax_scheme, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}ID").text = "VAT"

    price = etree.SubElement(inv_line, "{urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2}Price")
    etree.SubElement(price, "{urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2}PriceAmount", currencyID="EUR").text = f"{data['net_amount']:.2f}"

    return etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="UTF-8")

st.title("📂 PDF2UBL - Passerelle WinBooks / Virtual Invoice")
st.write("Convertissez vos factures PDF en XML UBL 2.1 avec affectation automatique des grilles TVA.")

uploaded_files = st.file_uploader("Déposez une ou plusieurs factures PDF", type=["pdf"], accept_multiple_files=True)

REGIMES = {
    "Taux standard 21%": {"rate": 21.0, "code": "S", "reason": None},
    "Taux réduit 6%": {"rate": 6.0, "code": "S", "reason": None},
    "Taux intermédiaire 12%": {"rate": 12.0, "code": "S", "reason": None},
    "Cocontractant (0% - Case 87)": {"rate": 0.0, "code": "K", "reason": "Autoliquidation - Art. 20 AR n°1"},
    "Intracommunautaire (0% - Case 86)": {"rate": 0.0, "code": "K", "reason": "Autoliquidation intracommunautaire"},
    "Exonéré / Art. 44 (0%)": {"rate": 0.0, "code": "E", "reason": "Exonéré TVA - Article 44"},
}

if uploaded_files:
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_out:
        for idx, file in enumerate(uploaded_files):
            st.divider()
            file_bytes = file.read()
            base_name = file.name.rsplit(".", 1)[0]
            st.subheader(f"📄 {file.name}")

            parsed = parse_pdf_data(file_bytes)

            col1, col2, col3, col4 = st.columns(4)
            with col1:
                inv_id = st.text_input("N° Facture", value=parsed["invoice_id"], key=f"id_{idx}")
                sup_name = st.text_input("Nom Fournisseur", value=parsed["supplier_name"], key=f"name_{idx}")
            with col2:
                sup_vat = st.text_input("TVA Fournisseur", value=parsed["supplier_vat"], key=f"vat_{idx}")
                vcs = st.text_input("Communication (VCS)", value=parsed["vcs"], key=f"vcs_{idx}")
            with col3:
                issue_d = st.text_input("Date Facture (AAAA-MM-JJ)", value=parsed["issue_date"], key=f"date_{idx}")
                due_d = st.text_input("Échéance (AAAA-MM-JJ)", value=parsed["due_date"], key=f"due_{idx}")
            with col4:
                regimes_keys = list(REGIMES.keys())
                def_idx = regimes_keys.index(parsed["regime"]) if parsed["regime"] in regimes_keys else 0
                chosen_regime = st.selectbox("Régime fiscal / Grille TVA", regimes_keys, index=def_idx, key=f"regime_{idx}")

                ttc = st.number_input("Total TTC (€)", value=parsed["gross_amount"], step=0.01, format="%.2f", key=f"ttc_{idx}")

                rate_info = REGIMES[chosen_regime]
                r = rate_info["rate"]

                if r > 0:
                    calc_ht = round(ttc / (1 + (r / 100)), 2)
                    calc_tva = round(ttc - calc_ht, 2)
                else:
                    calc_ht = ttc
                    calc_tva = 0.0

                htva = st.number_input("Montant HTVA (€)", value=calc_ht, step=0.01, format="%.2f", key=f"ht_{idx}")
                tva = st.number_input("Montant TVA (€)", value=calc_tva, step=0.01, format="%.2f", key=f"tva_{idx}")

            final_data = {
                "invoice_id": inv_id,
                "supplier_name": sup_name,
                "supplier_vat": sup_vat,
                "vcs": vcs,
                "issue_date": issue_d,
                "due_date": due_d,
                "rate": r,
                "tax_category_code": rate_info["code"],
                "exemption_reason": rate_info["reason"],
                "net_amount": htva,
                "tax_amount": tva,
                "gross_amount": ttc,
            }

            xml_content = generate_ubl_xml(final_data, file_bytes, file.name)
            zip_out.writestr(f"{base_name}.pdf", file_bytes)
            zip_out.writestr(f"{base_name}.xml", xml_content)

    st.success("Toutes les pièces sont prêtes.")
    st.download_button(
        label="📥 Télécharger le ZIP pour Virtual Invoice",
        data=zip_buffer.getvalue(),
        file_name="import_winbooks_ubl.zip",
        mime="application/zip",
    )
