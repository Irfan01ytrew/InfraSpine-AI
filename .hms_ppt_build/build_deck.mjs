import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { Presentation, PresentationFile } from '@oai/artifact-tool';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE = path.dirname(HERE);
const SKILL_DIR = 'C:\\Users\\10\\.codex\\plugins\\cache\\openai-primary-runtime\\presentations\\26.1112.11217\\skills\\presentations';
const FONT = 'Arial';
const W = 1280;
const H = 720;
const C = {
  ink: '#173B40',
  ink2: '#31565A',
  teal: '#28766F',
  mint: '#DCECE6',
  mint2: '#B9D8D0',
  cream: '#F5F1E8',
  paper: '#FFFEFA',
  gold: '#E9B75F',
  rust: '#C96D4C',
  muted: '#526568',
  gray: '#D7E0DC',
  white: '#FFFFFF',
  navy: '#102E34',
  paleGold: '#F6E9CC',
  paleRust: '#F3DED6',
};

const img = {};
for (const name of ['cover', 'pos', 'inventory', 'hospital', 'accounts']) {
  img[name] = new Uint8Array(await fs.readFile(path.join(HERE, 'assets', `${name}.png`)));
}

const deck = Presentation.create({ slideSize: { width: W, height: H } });

function shape(slide, name, pos, fill = 'none', lineFill = 'none', lineWidth = 0, geometry = 'rect', radius) {
  return slide.shapes.add({
    geometry,
    name,
    position: pos,
    fill,
    line: { style: 'solid', fill: lineFill, width: lineWidth },
    ...(radius ? { borderRadius: radius } : {}),
  });
}

function text(slide, name, value, pos, opts = {}) {
  const s = shape(slide, name, pos, 'none', 'none', 0, 'textbox');
  s.text = value;
  s.text.style = {
    typeface: FONT,
    fontSize: opts.size ?? 22,
    bold: opts.bold ?? false,
    color: opts.color ?? C.ink,
    alignment: opts.align ?? 'left',
    verticalAlignment: opts.valign ?? 'middle',
    wrap: 'square',
    autoFit: opts.fit ?? 'shrinkText',
    insets: opts.insets ?? 0,
    ...(opts.lineSpacing ? { lineSpacing: opts.lineSpacing } : {}),
  };
  return s;
}

function panel(slide, name, value, pos, opts = {}) {
  const s = shape(slide, name, pos, opts.fill ?? C.paper, opts.stroke ?? C.gray, opts.strokeWidth ?? 1, opts.geometry ?? 'roundRect', opts.radius ?? 18);
  if (value !== undefined && value !== '') {
    s.text = value;
    s.text.style = {
      typeface: FONT,
      fontSize: opts.size ?? 22,
      bold: opts.bold ?? false,
      color: opts.color ?? C.ink,
      alignment: opts.align ?? 'center',
      verticalAlignment: opts.valign ?? 'middle',
      wrap: 'square',
      autoFit: 'shrinkText',
      insets: opts.insets ?? { top: 12, right: 14, bottom: 12, left: 14 },
    };
  }
  return s;
}

function arrow(slide, name, x, y, w = 42, color = C.teal) {
  const s = shape(slide, name, { left: x, top: y, width: w, height: 30 }, color, 'none', 0, 'rightArrow');
  return s;
}

function line(slide, name, x1, y1, x2, y2, color = C.mint2, width = 3) {
  return slide.shapes.add({
    geometry: 'line', name,
    position: { left: x1, top: y1, width: x2 - x1, height: y2 - y1 },
    fill: 'none', line: { style: 'solid', fill: color, width },
  });
}

function addImage(slide, name, bytes, alt, pos, opts = {}) {
  return slide.images.add({
    blob: bytes,
    contentType: 'image/png',
    name,
    alt,
    fit: opts.fit ?? 'cover',
    position: pos,
    ...(opts.geometry ? { geometry: opts.geometry } : {}),
    ...(opts.radius ? { borderRadius: opts.radius } : {}),
    ...(opts.crop ? { crop: opts.crop } : {}),
  });
}

function footer(slide, number, dark = false) {
  const col = dark ? '#C4D8D4' : '#718184';
  text(slide, `footer-left-${number}`, 'INFRASPINE • DEMO GUIDE • 14 NOV 2026', { left: 64, top: 674, width: 500, height: 22 }, { size: 13, color: col });
  text(slide, `footer-right-${number}`, String(number).padStart(2, '0'), { left: 1180, top: 674, width: 40, height: 22 }, { size: 14, color: col, align: 'right', bold: true });
}

function base(titleText, subtitle, n, opts = {}) {
  const slide = deck.slides.add();
  const bg = opts.dark ? C.navy : C.cream;
  slide.background.fill = bg;
  if (!opts.noHeading) {
    text(slide, `heading-${n}`, titleText, { left: 64, top: 42, width: opts.titleWidth ?? 1110, height: 56 }, { size: opts.titleSize ?? 42, bold: true, color: opts.dark ? C.white : C.ink });
    if (subtitle) text(slide, `subtitle-${n}`, subtitle, { left: 66, top: 101, width: 1110, height: 32 }, { size: 19, color: opts.dark ? '#C4D8D4' : C.muted });
    shape(slide, `header-rule-${n}`, { left: 64, top: 143, width: 74, height: 5 }, opts.dark ? C.gold : C.teal, 'none', 0, 'roundRect', 3);
  }
  footer(slide, n, opts.dark);
  return slide;
}

function notes(slide, value) {
  slide.speakerNotes.text = `${value}\n\nSource: signed-in demo screens and safe form checks at https://hms-demo.infraspine.com/ (review dated 14 November 2026). Generated images are illustrative artwork, not screenshots of the application.`;
}

// 1 — Cover
{
  const s = deck.slides.add();
  s.background.fill = C.cream;
  addImage(s, 'cover-illustration', img.cover, 'Illustrative clinic reception, pharmacy, stock room and connected software', { left: 0, top: 0, width: W, height: H }, { fit: 'cover' });
  shape(s, 'cover-left-wash', { left: 0, top: 0, width: 600, height: H }, '#F5F1E8/88', 'none', 0);
  text(s, 'cover-kicker', 'ASAN ROMAN URDU GUIDE', { left: 78, top: 126, width: 435, height: 32 }, { size: 18, color: C.teal, bold: true });
  text(s, 'cover-title', 'InfraSpine\nPOS + Hospital', { left: 76, top: 176, width: 520, height: 186 }, { size: 56, color: C.ink, bold: true, valign: 'top' });
  text(s, 'cover-subtitle', 'Har module ka maqsad, entry ka tareeqa, aur final button ka asar — tasveeron aur seedhay flows ke saath.', { left: 80, top: 386, width: 450, height: 112 }, { size: 25, color: C.ink2, valign: 'top' });
  panel(s, 'cover-date', 'Demo ka jaiza: 14 November 2026', { left: 80, top: 548, width: 370, height: 52 }, { fill: C.ink, color: C.white, size: 18, bold: true, stroke: C.ink });
  notes(s, 'Yeh presentation InfraSpine ke signed-in demo par dekhe gaye POS/ERP aur Human Hospital screens ka aam-fahm tour hai. Yeh sirf hospital software nahi; retail sale, inventory, purchase aur accounts bhi is mein shamil hain. Har slide par mukhtasar baat hai. Speaker notes mein form fields, buttons, statuses, aur test ki had detail se likhi hai. Demo ke kuch workflows configuration ya backend error ki wajah se mukammal nahi chale; unhein verified result se alag bataya gaya hai.');
}

// 2 — Scope
{
  const s = base('Yeh sirf HMS nahi', 'Aik hi platform mein teen aapas mein jurray kaam', 2);
  const rows = [
    { y: 190, num: '01', tag: 'POS / RETAIL', title: 'Counter par sale', body: 'Item dhoondo → bill banao → payment lo → final charge par sale post hoti hai.', fill: C.mint },
    { y: 330, num: '02', tag: 'ERP / BUSINESS', title: 'Maal aur hisaab', body: 'Purchase, stock, suppliers, customers, branches, accounts aur business reports.', fill: C.paleGold },
    { y: 470, num: '03', tag: 'HUMAN HOSPITAL', title: 'Mareez ka safar', body: 'Registration, OPD, doctor, lab, pharmacy, IPD aur patient billing.', fill: C.paleRust },
  ];
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    panel(s, `scope-row-${i}`, '', { left: 72, top: r.y, width: 1136, height: 112 }, { fill: r.fill, stroke: r.fill, radius: 20 });
    text(s, `scope-number-${i}`, r.num, { left: 92, top: r.y + 24, width: 62, height: 50 }, { size: 32, bold: true, color: C.teal });
    text(s, `scope-tag-${i}`, r.tag, { left: 174, top: r.y + 12, width: 300, height: 26 }, { size: 15, bold: true, color: C.teal });
    text(s, `scope-title-${i}`, r.title, { left: 174, top: r.y + 40, width: 300, height: 40 }, { size: 27, bold: true });
    text(s, `scope-body-${i}`, r.body, { left: 496, top: r.y + 22, width: 660, height: 66 }, { size: 22, color: C.ink2 });
  }
  notes(s, 'Retail/POS bill aur counter transaction banata hai. ERP business side ko chalata hai: suppliers se maal, stock, ledgers aur reports. Human Hospital patient identity aur care-related workflows ko inhi financial/inventory records se jorta hai. “System kaam karta hai” aur “demo mein end-to-end test hua” alag baatein hain. Demo par retail form/ledger ke chand safe draft tests hue; hospital mein sirf synthetic patient registration tak test hua.');
}

// 3 — Data flow map
{
  const s = base('System ke andar data kahan jata hai?', 'Final “Post / Charge / Save” se pehle record aksar draft hota hai', 3);
  text(s, 'flow-retail-label', 'MAAL SE SALES TAK', { left: 72, top: 174, width: 300, height: 26 }, { size: 16, bold: true, color: C.teal });
  const top = [
    ['Product\n& price', 72, 216, C.mint], ['Purchase\n/ receipt', 300, 216, C.paleGold], ['Stock\n& batch', 528, 216, C.mint], ['POS\ninvoice', 756, 216, C.paleRust], ['Ledger\n& report', 984, 216, C.paleGold],
  ];
  for (let i = 0; i < top.length; i++) {
    const [label, x, y, fill] = top[i];
    panel(s, `retail-node-${i}`, label, { left: x, top: y, width: 186, height: 90 }, { fill, stroke: fill, size: 22, bold: true });
    if (i < top.length - 1) arrow(s, `retail-arrow-${i}`, x + 190, y + 28, 34, C.teal);
  }
  text(s, 'flow-hospital-label', 'PATIENT SE HOSPITAL BILL TAK', { left: 72, top: 360, width: 390, height: 26 }, { size: 16, bold: true, color: C.teal });
  const bot = [
    ['Service +\ndoctor setup', 72, 402, C.mint], ['Patient\nMRN / visit', 300, 402, C.paleGold], ['OPD /\nencounter', 528, 402, C.mint], ['Lab / drug /\nbed service', 756, 402, C.paleRust], ['Patient\nbill / pay', 984, 402, C.paleGold],
  ];
  for (let i = 0; i < bot.length; i++) {
    const [label, x, y, fill] = bot[i];
    panel(s, `hospital-node-${i}`, label, { left: x, top: y, width: 186, height: 90 }, { fill, stroke: fill, size: 21, bold: true });
    if (i < bot.length - 1) arrow(s, `hospital-arrow-${i}`, x + 190, y + 28, 34, C.teal);
  }
  text(s, 'flow-warning', 'Zaroori setup ya original record missing ho, to agla qadam ruk sakta hai.', { left: 94, top: 544, width: 1080, height: 48 }, { size: 23, bold: true, color: C.rust, align: 'center' });
  notes(s, 'Retail ka intended silsila: product/price catalog, supplier purchase order, physical receiving/purchase invoice, store stock, POS invoice/payment, phir account/stock reports. Order se stock nahi badhta; receipt ya invoice ke save/post par badal sakta hai. POS hold final sale nahi hota; Charge final sale hota hai. Hospital ka intended silsila: pehle billable service aur doctor/session; phir patient MRN, appointment ya walk-in check-in, OPD queue, fee clearance, clinical encounter, lab/pharmacy/IPD service, phir Patient Billing. Current demo mein doctor/session configured nahi the aur service save request 502 fail hui, is liye hospital chain ko mukammal test nahi kar paye.');
}

// 4 — Dashboard
{
  const s = base('Dashboard: aik nazar mein halat', 'Date range aur branch badlein; totals us ke mutabiq dikhte hain', 4);
  const metrics = [
    { x: 72, big: 'Rs. 200', label: 'Selected period sales', fill: C.mint },
    { x: 354, big: '199', label: 'Stock units', fill: C.paleGold },
    { x: 636, big: 'Rs. 39,800', label: 'Batch-cost stock value', fill: C.mint },
    { x: 918, big: '0', label: 'Invoices today', fill: C.paleRust },
  ];
  metrics.forEach((m, i) => {
    panel(s, `metric-${i}`, '', { left: m.x, top: 188, width: 258, height: 156 }, { fill: m.fill, stroke: m.fill });
    text(s, `metric-big-${i}`, m.big, { left: m.x + 18, top: 214, width: 222, height: 60 }, { size: 34, bold: true, align: 'center' });
    text(s, `metric-label-${i}`, m.label, { left: m.x + 18, top: 279, width: 222, height: 42 }, { size: 18, color: C.ink2, align: 'center' });
  });
  text(s, 'dashboard-date', 'Selected period: 1–14 Nov 2026  •  Branch: Main Branch', { left: 75, top: 368, width: 1050, height: 30 }, { size: 18, bold: true, color: C.teal });
  line(s, 'stock-progress-line', 92, 456, 1164, 456, C.gray, 10);
  line(s, 'stock-progress-low', 92, 456, 1130, 456, C.rust, 10);
  panel(s, 'reorder-chip', '199 on hand', { left: 94, top: 420, width: 204, height: 70 }, { fill: C.paleRust, stroke: C.paleRust, size: 20, bold: true });
  panel(s, 'reorder-target', '200 reorder level', { left: 964, top: 420, width: 208, height: 70 }, { fill: C.ink, stroke: C.ink, color: C.white, size: 19, bold: true });
  text(s, 'dashboard-note', 'Item reorder limit se 1 unit neeche tha — “Low stock” signal.', { left: 100, top: 520, width: 1060, height: 38 }, { size: 22, color: C.rust, align: 'center', bold: true });
  text(s, 'dashboard-click-note', 'Cards: invoice list • low/expiry stock • recent sales • held bills • tender mix • top items', { left: 80, top: 578, width: 1120, height: 38 }, { size: 18, color: C.ink2, align: 'center' });
  notes(s, 'Dashboard business ka summary hai. Period options Today, Yesterday, This week, Last week, This month, Last month, pichle 3/6 months, This year, Last year ya Custom. Branch filter selected branch ke numbers dikhata hai. Demo Main Branch, selected period 1–14 November 2026: total sales Rs 200, today net sale Rs 0, aaj invoices 0; inventory: 1 active product, 199 units, Rs 39,800 batch-cost value, reorder 200; near-expiry 0, zero stock 0, negative stock 0, purchase total Rs 0. Hospital dashboard baad mein check kiya: synthetic test patient ki wajah se registered today 1; OPD waiting 0, inpatients 0, admissions 0, beds 0/0, lab orders 0, prescriptions 0 aur aaj invoiced Rs 0.00. Cards/charts mostly detail screens par le jate hain. Date range dekhay baghair comparison na karein.');
}

// 5 — POS
{
  const s = base('POS: counter par item se receipt tak', 'Cashier ka bill “Charge” se pehle draft rehta hai', 5, { titleWidth: 615 });
  addImage(s, 'pos-photo', img.pos, 'Illustrative pharmacy cashier scanning a product barcode at a till', { left: 722, top: 170, width: 486, height: 390 }, { fit: 'cover', geometry: 'roundRect', radius: 24, crop: { left: 0.22, top: 0, right: 0, bottom: 0 } });
  const steps = [
    ['1', 'Dhoondo', 'Name / SKU / brand / barcode • F2'],
    ['2', 'Bill line', 'Qty, unit price, discount, line total'],
    ['3', 'Payment', 'Cash / Card / Transfer / Split'],
    ['4', 'Check', 'Due, received amount, change'],
  ];
  steps.forEach((r, i) => {
    const y = 183 + i * 93;
    panel(s, `pos-step-number-${i}`, r[0], { left: 78, top: y, width: 50, height: 50 }, { fill: i === 3 ? C.paleGold : C.mint, stroke: i === 3 ? C.paleGold : C.mint, size: 24, bold: true });
    text(s, `pos-step-title-${i}`, r[1], { left: 144, top: y, width: 150, height: 32 }, { size: 24, bold: true });
    text(s, `pos-step-body-${i}`, r[2], { left: 144, top: y + 37, width: 520, height: 42 }, { size: 18, color: C.ink2 });
  });
  panel(s, 'pos-total-test', 'Rs. 200 due  +  Rs. 500 received  =  Rs. 300 change', { left: 74, top: 568, width: 1138, height: 58 }, { fill: C.ink, stroke: C.ink, color: C.white, size: 21, bold: true });
  notes(s, 'POS search name, SKU, brand, barcode se item dhoondta hai. Search results mein item name/code, UOM, price, stock dikh sakte hain. Customer walk-in ya registered account hota hai. Customer/price book bill par asar kar sakti hai. Current lines mein qty, unit price, discount aur total; order summary mein subtotal, bill discount aur tax/GST. Cash method par cash book/account chuna ja sakta hai. Cash received ke baad change due calculate hota hai. Test mein name “Antibiotics” se result nahi mila; SKU “0001” se Rs 200 item aur stock 199 mila. Qty 1 ka test draft bana; Rs 500 tender par Rs 300 change dikhaya. Draft line remove kar di; Charge/Charge & Print nahi dabaya, is liye test ke dauran nayi sale, payment ya stock movement post nahi hua. Buttons: F12 Charge & Print final sale; F8 Hold Sale parks a draft; F5 New Bill; F6 Held Bills; F7 Process Return; F1 shortcut help. Register shift open hona zaroori ho sakta hai. Register Shifts mein open/close, cash in/out aur counted-vs-expected cash control hota hai.');
}

// 6 — Sales
{
  const s = base('Sales: quote, invoice, return', 'Har document ka stock aur paisay par alag asar hota hai', 6);
  const y = 236;
  const nodes = [
    ['Quotation', 'Estimate • stock nahin badalta', C.mint],
    ['Sales Invoice', 'Posted bill • stock/ledger asar', C.paleGold],
    ['Sale Return', 'Original bill • wapsi/refund', C.paleRust],
  ];
  nodes.forEach((n, i) => {
    const x = [84, 476, 868][i];
    panel(s, `sales-doc-${i}`, '', { left: x, top: y, width: 304, height: 158 }, { fill: n[2], stroke: n[2] });
    text(s, `sales-doc-title-${i}`, n[0], { left: x + 16, top: y + 24, width: 272, height: 38 }, { size: 25, bold: true, align: 'center' });
    text(s, `sales-doc-desc-${i}`, n[1], { left: x + 22, top: y + 74, width: 260, height: 54 }, { size: 19, color: C.ink2, align: 'center' });
  });
  arrow(s, 'sales-quote-to-invoice', 405, y + 61, 54, C.teal);
  arrow(s, 'sales-invoice-to-return', 797, y + 61, 54, C.rust);
  text(s, 'sale-form-fields-head', 'Invoice / quote par aam tor par:', { left: 86, top: 436, width: 430, height: 34 }, { size: 22, bold: true });
  text(s, 'sale-form-fields', 'Date • customer • salesman • store • item code • UOM • qty • price • discount • payment mode • due date', { left: 86, top: 478, width: 1100, height: 55 }, { size: 20, color: C.ink2 });
  panel(s, 'customer-master-callout', 'Customer master: contact + price list + credit limit/days + active/blocked status', { left: 88, top: 556, width: 1100, height: 58 }, { fill: C.ink, stroke: C.ink, color: C.white, size: 20, bold: true });
  notes(s, 'Quotation mein quote date, valid-until, customer/mobile, salesman, store, item code/product, UOM, qty, price aur discount aa sakte hain. Save Quote quote record banata hai; accepted quote ko Convert to Invoice se invoice banaya ja sakta hai. Quote khud stock/payment post nahi karta. Sales Invoice list posted documents dikhati hai; new bill mein date, branch/store, customer, salesman, payment mode (cash/card/bank transfer/account), due date, discount, received amount aur item lines ho sakte hain. Demo screen ka behavior batata tha ke invoice save/post stock par asar kar sakta hai. Invoice view/reprint/return/void actions dekhna. Sale Return ke liye original invoice load, sold line, return qty (sold qty se zyada nahi), reason/reference aur refund method chunein; posting stock/accounts ko affect kar sakti hai. Customer master code/name/category/contact, address/email, price list, credit active/blocked, credit limit/day rakhta hai; customer invoice history/receivable se judta hai. Veterinary setup owner/pet profile link bhi support kar sakta hai.');
}

// 7 — Inventory
{
  const s = base('Inventory: stock ka asal hisaab', 'Har in/out movement audited stock ledger mein trace honi chahiye', 7, { titleWidth: 640 });
  addImage(s, 'inventory-photo', img.inventory, 'Illustrative store worker scanning a carton in a warehouse', { left: 752, top: 170, width: 456, height: 316 }, { fit: 'cover', geometry: 'roundRect', radius: 22, crop: { left: 0.22, top: 0, right: 0, bottom: 0 } });
  text(s, 'inv-state-label', 'DEMO ITEM', { left: 80, top: 188, width: 230, height: 28 }, { size: 16, bold: true, color: C.teal });
  text(s, 'inv-state-number', '199', { left: 78, top: 220, width: 220, height: 78 }, { size: 65, bold: true, color: C.ink });
  text(s, 'inv-state-copy', 'units on hand\nreorder level: 200', { left: 86, top: 300, width: 250, height: 74 }, { size: 21, color: C.ink2 });
  panel(s, 'inv-status', 'LOW • limit se 1 kam', { left: 78, top: 397, width: 290, height: 56 }, { fill: C.paleRust, stroke: C.paleRust, color: C.rust, size: 20, bold: true });
  line(s, 'inv-bottom-line', 82, 530, 1198, 530, C.teal, 4);
  const op = [['RECEIVE', 'maal andar'], ['ADJUST', 'approved farq'], ['TRANSFER', 'store badalna'], ['COUNT', 'ginti milana'], ['BATCH', 'lot + expiry']];
  op.forEach((a, i) => {
    const x = 76 + i * 228;
    panel(s, `inventory-op-dot-${i}`, a[0], { left: x, top: 501, width: 150, height: 58 }, { fill: i % 2 ? C.paleGold : C.mint, stroke: 'none', size: 17, bold: true });
    text(s, `inventory-op-label-${i}`, a[1], { left: x - 6, top: 570, width: 164, height: 36 }, { size: 17, color: C.ink2, align: 'center' });
  });
  notes(s, 'Stock Overview mein product, on-hand, reorder, batches, nearest expiry, stock value aur status. Filters in-stock, low, expiring, out-of-stock, negative. Receive Stock: direct ya purchase-order ke against mode, receiving store, supplier, supplier invoice/reference, note, item qty/cost; Post receipt se quantity badalti hai. Stock Adjustment: product/batch/store, current qty, plus/minus, reason; save audited movement bana sakta hai. Transfer: source/destination, item/batch, qty, send/receive/transfer post; demo mein doosra store configured nahi tha. Stock Count: full location ya cycle (selected products), physical qty, variance review, approve/save. Batch & Expiry expiry filters se risk dikhata hai; 90 din ke andar expiring stock 0 tha. Opening Stock initial migration balance banata hai; store/product qty ya CSV/Excel import. Stock ka authoritative amount stock ledger se trace karein; sirf overview card ko adjustment ka badal na samjhein.');
}

// 8 — Catalog
{
  const s = base('Product Catalog: item ko pehchan aur qeemat dein', 'Master data pehle saaf ho, tab POS aur inventory asani se kaam kare', 8);
  panel(s, 'catalog-root', 'PRODUCT', { left: 500, top: 176, width: 280, height: 70 }, { fill: C.ink, stroke: C.ink, color: C.white, size: 25, bold: true });
  line(s, 'catalog-stem', 640, 246, 640, 288, C.teal, 4);
  line(s, 'catalog-branch', 186, 288, 1094, 288, C.teal, 4);
  const branches = [
    ['GROUP / CATEGORY', 'Product families\nsub-category tak', 72, C.mint],
    ['IDENTITY', 'Code • barcode • name\nbrand • generic', 362, C.paleGold],
    ['UNIT / PRICE', 'Base + buy/sell UOM\nconversion • cost • price', 652, C.mint],
    ['STOCK PLACE', 'Default store\nrack / shelf • batches', 942, C.paleRust],
  ];
  branches.forEach((b, i) => {
    const cx = b[2] + 120;
    line(s, `catalog-drop-${i}`, cx, 288, cx, 325, C.teal, 3);
    panel(s, `catalog-branch-${i}`, '', { left: b[2], top: 326, width: 240, height: 120 }, { fill: b[3], stroke: b[3] });
    text(s, `catalog-branch-title-${i}`, b[0], { left: b[2] + 10, top: 342, width: 220, height: 30 }, { size: 17, color: C.teal, bold: true, align: 'center' });
    text(s, `catalog-branch-body-${i}`, b[1], { left: b[2] + 14, top: 378, width: 212, height: 54 }, { size: 18, color: C.ink, align: 'center' });
  });
  text(s, 'catalog-grid-header', 'Product record mein:', { left: 82, top: 484, width: 285, height: 30 }, { size: 21, bold: true });
  text(s, 'catalog-grid-fields', 'Item type: Product / Raw Material / Service  •  manufacturer/supplier  •  active  •  tax/GST & HSN  •  custom fields', { left: 82, top: 522, width: 1120, height: 50 }, { size: 19, color: C.ink2 });
  panel(s, 'catalog-price-change', '', { left: 82, top: 574, width: 1116, height: 66 }, { fill: C.paleGold, stroke: C.paleGold });
  text(s, 'catalog-price-change-copy', 'Bulk Price Change → selected product prices ek batch mein update\nFuture bills nayi price lete hain; old posted invoices nahi badalte', { left: 100, top: 580, width: 1080, height: 54 }, { size: 19, bold: true, color: C.ink, align: 'center', fit: 'none' });
  notes(s, 'Products screen product master ka central record hai. Fields include auto code, item type Product/Raw Material/Service, description, barcode, Unicode name, product group/category/subcategory, default store, rack/shelf, generic, color, manufacturer/supplier, active, optional opening stock. Stock & Price Info purchase price, average cost, wholesale/sale price aur item stock values. UOM Details base, purchase, sales units; conversion se carton ko pieces mein convert. Stores Stock per-store qty. Custom fields Brand, Tax/GST, HSN. Product Groups broad family; Categories group ke andar category/subcategory; group pehle select hota hai. Units of Measure unit type Count/Mass/Volume/Time/Other, name/code, integer-only or decimal. Catalog central Categories/Brands/Unit Rules manage karta hai; item type ke mutabiq categories. Product grid search, columns/layout save, import/export, copy, refresh, preview/print. Delete se pehle references aur recovery behavior samjhein. Bulk Price Change multiple selected product purchase/sale price update; new price future invoices ke liye hai, purane posted invoice recalculate nahi hote.');
}

// 9 — Purchase
{
  const s = base('Purchase: supplier se store tak', 'Order, receipt aur supplier bill teen alag events hain', 9);
  const xs = [90, 334, 578, 822, 1066];
  const st = [
    ['SUPPLIER', 'vendor +\nterms', C.mint],
    ['PURCHASE ORDER', 'kya mangwaya', C.paleGold],
    ['RECEIVE STOCK', 'asal qty aayi', C.mint],
    ['PURCHASE INVOICE', 'supplier ka bill', C.paleRust],
    ['AP / LEDGER', 'kitna dena hai', C.paleGold],
  ];
  st.forEach((v, i) => {
    panel(s, `purchase-step-${i}`, '', { left: xs[i], top: 232, width: 150, height: 154 }, { fill: v[2], stroke: v[2] });
    text(s, `purchase-step-title-${i}`, v[0], { left: xs[i] + 5, top: 250, width: 140, height: 40 }, { size: 16, bold: true, align: 'center', color: C.teal });
    text(s, `purchase-step-body-${i}`, v[1], { left: xs[i] + 10, top: 302, width: 130, height: 50 }, { size: 21, bold: true, align: 'center' });
    if (i < 4) arrow(s, `purchase-arrow-${i}`, xs[i] + 170, 294, 52, C.teal);
  });
  text(s, 'purchase-order-state', 'PO = mangwane ka irada. Is se akelay stock receive nahi hota.', { left: 90, top: 430, width: 1098, height: 42 }, { size: 22, bold: true, color: C.rust, align: 'center' });
  text(s, 'purchase-fields', 'PO: expected date + supplier + receiving store + item lines    |    Invoice: vendor invoice no. + due date + tax + cash/bank/credit', { left: 104, top: 492, width: 1072, height: 72 }, { size: 19, color: C.ink2, align: 'center' });
  panel(s, 'purchase-return', '', { left: 116, top: 574, width: 1048, height: 66 }, { fill: C.ink, stroke: C.ink });
  text(s, 'purchase-return-copy', 'Purchase Return → received maal supplier ko wapas\nPosting par stock aur supplier payable update hotay hain', { left: 136, top: 580, width: 1008, height: 54 }, { size: 19, bold: true, color: C.white, align: 'center', fit: 'none' });
  notes(s, 'Purchase Order supplier ko planned order hai. Receiving store, supplier, order date, expected delivery, notes, product/qty/value enter; Draft/Ordered status. Maal aane par PO ke against receipt. Test mein New Order form dekha, cancel kiya; 0 orders saved. Purchase Invoice asal supplier bill: supplier, invoice/due dates, supplier invoice no, optional PO, receiving store, product lines, taxable amount/input GST, payment mode Credit/Cash/Bank. Record stock aur AP (payable) ko affect kar sakta hai. Purchase Return original received goods/bill se line/qty, reason/reference; post par stock kam aur supplier ledger adjust. Supplier master vendor code, name, contact, payment terms; purchases/AP us se link. Receipt vs Invoice Reconciliation report compares ordered, received at actual cost and invoiced; report khud accounting posting nahi banati.');
}

// 10 — Accounts masters
{
  const s = base('Accounts: posting se pehle maps tayyar karein', 'Transaction ko sahi ledger se jorna accounts ka bunyadi kaam hai', 10, { titleWidth: 1110 });
  addImage(s, 'accounts-photo', img.accounts, 'Illustrative accountant reviewing a ledger, bank records and reports', { left: 770, top: 170, width: 438, height: 382 }, { fit: 'cover', geometry: 'roundRect', radius: 22, crop: { left: 0.26, top: 0, right: 0, bottom: 0 } });
  const items = [
    ['Account Groups', 'Capital • Cash • Bank • Sales • Debtors/Creditors'],
    ['Chart of Accounts', 'Har ledger ka parent group aur structure'],
    ['Branch Accounts', 'Branch-wise cash / bank / sales GL mapping'],
    ['Revenue Accounts', 'Product/service category → sale income ledger'],
    ['Cost / Profit Centres', 'Department ya profit area ka tag'],
  ];
  items.forEach((a, i) => {
    const y = 184 + i * 69;
    text(s, `account-master-name-${i}`, a[0], { left: 84, top: y, width: 220, height: 30 }, { size: 20, bold: true, color: C.teal });
    text(s, `account-master-desc-${i}`, a[1], { left: 300, top: y, width: 440, height: 48 }, { size: 18, color: C.ink2 });
  });
  panel(s, 'opening-balance', 'Opening Balance = migration waqt purana chalta hua balance', { left: 84, top: 548, width: 1118, height: 52 }, { fill: C.paleGold, stroke: C.paleGold, size: 19, bold: true });
  notes(s, 'Accounts modules automatic POS/purchase postings aur manual vouchers ko double entry mein record karte hain. Accounts Group ledger ko statement families (Capital, Cash, Bank, Debtors, Creditors, Sales, Purchases) mein rakhta hai. Chart of Account hierarchical list; Normal Account ko suitable group se attach karein. Cost & Profit Centres voucher line ko department/profit-area tag dete hain; branch se alag dimension, optional branch restriction. Branch Accounts branch ke cash, bank, sales aur posting purpose GL map karte hain. Revenue Accounts product/service category sale income GL se map karte hain; unmapped category branch Sales account mein ja sakti hai. Add Opening Balance system migration ke waqt pehle se chala aa raha balance enter karta hai. Mapping clear na ho to sale/payment ko final karne se pehle accounts admin se confirm karein.');
}

// 11 — Vouchers and cash
{
  const s = base('Vouchers: har debit ka aik muqabil credit', 'Cash, bank ya journal ke zariye manual accounting entries', 11);
  panel(s, 'debit-side', 'DEBIT', { left: 94, top: 196, width: 414, height: 66 }, { fill: C.mint, stroke: C.mint, size: 26, bold: true });
  panel(s, 'credit-side', 'CREDIT', { left: 772, top: 196, width: 414, height: 66 }, { fill: C.paleGold, stroke: C.paleGold, size: 26, bold: true });
  text(s, 'vouchers-vs', '=', { left: 586, top: 202, width: 88, height: 56 }, { size: 48, bold: true, color: C.teal, align: 'center' });
  const vouchers = [
    ['Cash Payment', 'cash kam • expense/account debit'],
    ['Cash Received', 'cash zyada • source account credit'],
    ['Bank Payment / Received', 'bank book ke zariye paisa andar/bahar'],
    ['Journal Voucher', 'cash/bank ke baghair ledger adjustment'],
  ];
  vouchers.forEach((v, i) => {
    const y = 306 + i * 70;
    text(s, `voucher-kind-${i}`, v[0], { left: 108, top: y, width: 300, height: 34 }, { size: 21, bold: true });
    text(s, `voucher-effect-${i}`, v[1], { left: 412, top: y, width: 748, height: 34 }, { size: 19, color: C.ink2 });
    if (i < 3) line(s, `voucher-divider-${i}`, 108, y + 48, 1168, y + 48, C.gray, 1);
  });
  panel(s, 'journal-draft-test', '', { left: 106, top: 574, width: 1064, height: 66 }, { fill: C.ink, stroke: C.ink });
  text(s, 'journal-draft-test-copy', 'TEST: Rs. 1 debit + Rs. 1 credit → Balanced\nDraft discard hua; koi voucher post nahin hua', { left: 128, top: 580, width: 1020, height: 54 }, { size: 19, bold: true, color: C.white, align: 'center', fit: 'none' });
  notes(s, 'Cash Payment: date, cash book, pay-to account, amount, narration. Cash account credit hota hai, selected expense/account debit. Posted mistake ko reversal se theek karein. Cash Received: cash-in-hand debit, source account credit. Bank Payment: bank book credit, expense/account debit. Bank Received: bank book debit, source credit. Journal Voucher cash/bank ke baghair adjustment; date, narration, account, debit, credit, particulars line-wise; total debit=total credit. Test: Rs 1 expense debit, Rs 1 sales credit par “Balanced” aur “Post journal” enabled; back to list kiya; koi voucher post nahi. Pending Approvals maker-checker queue mein wait karte vouchers ko approve/reject karti hai. Expenses petty cash/till: category, amount, cash/card/bank transfer, note/reference, GL mapping; cash expense open register shift mein post ho sakta hai. Register Shifts opening/closing, cash in/out aur counted-vs-expected cash; demo mein open shift nahi. Fiscal Compliance e-invoice status queued/filed/rejected/failed, retry/audit. Bank Reconciliation bank statement ko GL/bank book se match; farq adjust, phir finalize. Inter-branch Transfers cash/bank approval-send-receive-reconcile lifecycle.');
}

// 12 — AR/AP periods and assets
{
  const s = base('Accounts ke baqi controls', 'Receivable, payable, period aur assets — sab ka apna control', 12);
  const lanes = [
    { y: 180, label: 'PAISA DENA / LENA', title: 'AR + AP', body: 'Customer Ledgers / AR Aging: humein kitna aur kab se lena hai. Supplier Ledgers: vendor ko kitna dena hai. AP Adjustments: supplier debit/credit note.', fill: C.mint },
    { y: 320, label: 'PERIOD BANDH', title: 'Fiscal periods', body: 'Open = posting chal sakti. Closed/Locked = nayi ya backdated posting rukti. Year-end result Retained Earnings mein jata hai; reopen reversal banata hai.', fill: C.paleGold },
    { y: 460, label: 'PLAN AUR FIXED ASSET', title: 'Budget + Assets', body: 'Budget vs Actual: approved plan aur posted GL. Asset Register: cost, useful life, salvage, depreciation, disposal. Depreciation Run journal post karta hai.', fill: C.paleRust },
  ];
  lanes.forEach((l, i) => {
    panel(s, `account-controls-${i}`, '', { left: 76, top: l.y, width: 1128, height: 112 }, { fill: l.fill, stroke: l.fill });
    text(s, `account-controls-label-${i}`, l.label, { left: 96, top: l.y + 15, width: 260, height: 26 }, { size: 15, color: C.teal, bold: true });
    text(s, `account-controls-title-${i}`, l.title, { left: 96, top: l.y + 44, width: 246, height: 42 }, { size: 26, bold: true });
    text(s, `account-controls-body-${i}`, l.body, { left: 360, top: l.y + 16, width: 812, height: 80 }, { size: 18, color: C.ink2 });
  });
  notes(s, 'Customer Ledgers / AR Aging show amounts customers owe and how long overdue, with drill-down. Supplier Ledgers show payable statements. AP Adjustments post balanced debit/credit notes with selected offset account. Fiscal Year & Periods states Open, Closed, Locked; closed/locked periods block new/backdated posts. Budgets target fiscal year/period, GL, optional branch and cost/profit centre; approved budget stays locked until audited unlock. Year-End Close validates, transfers net result to Retained Earnings, hard-locks periods; reopen creates linked reversal. Asset Categories map asset, accumulated depreciation, depreciation expense and disposal gain/loss GL accounts. Fixed Asset Register stores cost, useful life, salvage value, status, capitalization, depreciation history, disposal or write-off. Depreciation Runs preview/post period depreciation and GL journal; duplicate asset/period charge is prevented.');
}

// 13 — Financial reports
{
  const s = base('Reports: jawab dhoondo, transaction nahin banao', 'Pehle se saved postings/documents ko filter karke read-only summary', 13);
  const stages = [
    ['1', 'Cash / bank', 'Cash Book • Bank Book • Daily Business Activity', C.mint],
    ['2', 'Ledger detail', 'Journal Ledger • Account Ledger • All Ledger Summary', C.paleGold],
    ['3', 'Check / explain', 'Trial Balance • AR Aging • Expense Summary • Accounts Group', C.mint],
    ['4', 'Business result', 'Income Statement • Balance Sheet • Cash Flow • P&L by centre', C.paleRust],
  ];
  stages.forEach((r, i) => {
    const x = 78 + i * 292;
    panel(s, `financial-report-${i}`, '', { left: x, top: 220, width: 252, height: 246 }, { fill: r[3], stroke: r[3] });
    text(s, `financial-report-num-${i}`, r[0], { left: x + 20, top: 236, width: 46, height: 46 }, { size: 28, color: C.teal, bold: true });
    text(s, `financial-report-title-${i}`, r[1], { left: x + 20, top: 294, width: 212, height: 44 }, { size: 22, bold: true });
    text(s, `financial-report-body-${i}`, r[2], { left: x + 20, top: 348, width: 212, height: 94 }, { size: 17, color: C.ink2, valign: 'top' });
  });
  text(s, 'financial-report-filters', 'Date • branch • account • centre • print preview (jahan available ho)', { left: 104, top: 512, width: 1072, height: 46 }, { size: 21, bold: true, color: C.teal, align: 'center' });
  text(s, 'financial-report-extra', 'Budget vs Actual • Transaction Activity Chart • Inventory Reconciliation', { left: 118, top: 576, width: 1044, height: 40 }, { size: 19, color: C.ink2, align: 'center' });
  notes(s, 'Reports transaction entry screens nahi hain. Date/branch/account/product filters se saved ledger/document lines ka summary/detail banate hain. Accounting & Financial reports: Cash Book (cash receipts/payments), Bank Book, AR Aging buckets, Journal Ledger chronological posted cash/bank/journal/opening vouchers, Account Ledger one ledger opening/period/closing, Accounts Group roll-up, Expense Summary category/period, All Ledger Summary opening/debit/credit/closing, Transaction Activity Chart, Daily Business Activity, Trial Balance opening/period/closing debit-credit, Income Statement trading profit/loss with print preview, P&L by Cost/Profit Centre posted GL lines, Budget vs Actual (monthly/YTD/annual, posted GL actual), Balance Sheet date-wise assets/liabilities/net worth, Cash Flow Statement operating/investing/financing from posted cash/bank books. Inventory Reconciliation read-only lot carrying value vs stock movement ledger vs inventory GL; exceptions include missing cost, lot mismatch, negative lot, rounding residue and transfer-link issues.');
}

// 14 — Inventory, sales, purchase reports
{
  const s = base('Stock aur trade reports', 'Aik sawal chunein: quantity, customer, category, salesman ya supplier?', 14);
  const cols = [
    { x: 76, color: C.mint, title: 'INVENTORY', head: 'Stock kitna hai?', body: 'Product Stock Ledger\nAll Product List with Stock\nStock & Sales Analysis\nStock Valuation by Category\nRe-Order Level Report\nSize & Colour Report' },
    { x: 462, color: C.paleGold, title: 'SALES', head: 'Kis ne kya khareeda?', body: 'Sales Day-Book • DSR\nSales by Products / Products by Customer\nSales by Services\nSales by Salesman\nMonthly / Multi-Level Summary\nDead Items • Bulk Invoice Print\nGodown Issuing Summary' },
    { x: 848, color: C.paleRust, title: 'PURCHASE', head: 'Kahan se kitna aaya?', body: 'Purchase Day-Book\nPurchases by Category / Products\nPurchases by Supplier\nMonthly Purchase Analysis\nOrder Level Report\nReceipt vs Invoice Reconciliation' },
  ];
  cols.forEach((c, i) => {
    panel(s, `report-column-${i}`, '', { left: c.x, top: 188, width: 352, height: 408 }, { fill: c.color, stroke: c.color });
    text(s, `report-col-title-${i}`, c.title, { left: c.x + 24, top: 210, width: 304, height: 26 }, { size: 15, bold: true, color: C.teal });
    text(s, `report-col-head-${i}`, c.head, { left: c.x + 24, top: 246, width: 304, height: 42 }, { size: 24, bold: true });
    text(s, `report-col-body-${i}`, c.body, { left: c.x + 26, top: 302, width: 300, height: 262 }, { size: 18, color: C.ink2, valign: 'top' });
  });
  notes(s, 'Inventory reports: Product Stock Ledger product opening/in/out/closing quantity from audited stock ledger; Stock & Sales Analysis stock movement, sold qty and stored invoice values; All Product List with Stock; Stock Valuation by Category; Product Sales by Customer with sold/refunded qty and stored invoice amounts; Re-Order Level Report; Size & Colour Report with net units and low-stock sizes. Sales: Bulk Invoice Printing posted invoices by date, selected/all receipts; Sales by Services (Retail/Veterinary/Human Hospital; stored sale/return/void lines, no patient/customer detail); Godown Issuing Summary; Sales Day-Book posted invoices/returns; Sales Summary by Products; Daily Sales Report (invoice count/returns/stored total); Multi-Level Sales Summary by group/category/subcategory from invoice snapshots; Sales by Products (sold/returned qty/stored amounts); Monthly Sales Analysis; Dead Items Sales no posted sale in selected period; Sales by Salesman completing user/salesperson. Purchase: Purchase Day-Book receipts/returns; Purchases by Category; by Products; by Supplier; Monthly Purchase Analysis; Order Level Report ordered vs received; Receipt vs Invoice Reconciliation compares order, actual received cost, supplier invoice, but creates no accounting entry. Reports are point-in-time views; check date range, branch and whether returns/voids are included.');
}

// 15 — Barcodes and service catalog
{
  const s = base('Barcodes aur Service Catalog', 'Barcode label item ko pehchanta hai; service record billable kaam ko', 15);
  panel(s, 'barcode-label-art', '', { left: 92, top: 198, width: 350, height: 320 }, { fill: C.paper, stroke: C.ink, strokeWidth: 2, radius: 10 });
  text(s, 'barcode-label-name', 'PRODUCT • PRICE', { left: 118, top: 220, width: 298, height: 32 }, { size: 16, color: C.teal, bold: true, align: 'center' });
  const bars = [14, 6, 9, 4, 12, 5, 8, 4, 15, 7, 5, 12, 4, 9, 5, 13, 5, 9, 4, 11, 6, 14, 4, 8, 6, 13];
  let xx = 132;
  bars.forEach((w, i) => {
    shape(s, `barcode-bar-${i}`, { left: xx, top: 284, width: w, height: i % 4 === 0 ? 132 : 114 }, C.ink, 'none', 0);
    xx += w + (i % 3 === 0 ? 4 : 2);
  });
  text(s, 'barcode-label-code', '0001  •  scan ya SKU se lookup', { left: 116, top: 436, width: 304, height: 40 }, { size: 18, color: C.ink2, align: 'center' });
  text(s, 'barcode-howto', 'Multiple Barcodes / Barcode Printing: item, label quantity, 1–3 columns, optional price → preview → print.', { left: 88, top: 544, width: 368, height: 76 }, { size: 18, color: C.ink2, align: 'center' });
  line(s, 'service-divider', 490, 185, 490, 626, C.gray, 2);
  text(s, 'service-flow-title', 'SERVICE KI SETUP CHAIN', { left: 544, top: 197, width: 620, height: 28 }, { size: 16, color: C.teal, bold: true });
  const serv = [
    ['Category', 'Consultation / Lab'], ['Service', 'Code • time • price • tax'], ['Use', 'Booking • POS • branch'],
  ];
  serv.forEach((a, i) => {
    const x = 536 + i * 210;
    panel(s, `service-step-${i}`, `${a[0]}\n${a[1]}`, { left: x, top: 258, width: 182, height: 122 }, { fill: i === 1 ? C.paleGold : C.mint, stroke: i === 1 ? C.paleGold : C.mint, size: 18, bold: i === 0 });
    if (i < 2) arrow(s, `service-arrow-${i}`, x + 184, 304, 28, C.teal);
  });
  panel(s, 'service-blocker', 'DEMO TEST: category save hui; billable service save par 502 error', { left: 536, top: 426, width: 624, height: 62 }, { fill: C.paleRust, stroke: C.paleRust, color: C.rust, size: 19, bold: true });
  text(s, 'service-booking-note', 'Booking OFF = appointment mein nahin. POS ON = checkout mein item mil sakta hai.', { left: 538, top: 518, width: 620, height: 60 }, { size: 19, color: C.ink2, align: 'center' });
  notes(s, 'Multiple Barcodes kai products ke labels ek batch mein banata hai: search item, qty per item, single/double/three-column layout, optional sale price, preview then print. Barcode Printing barcode ya product search se label list, name, sale price, label quantity, 1/2/3 columns. Print actual printer/output bhej sakta hai; test mein print nahi kiya. Service Catalog stores billable service name/category/code/duration/price/tax/branch availability. Hospital booking and Patient Billing catalog se service/fee le sakte; Available in POS POS mein search/scan/sell aur branch offline catalog mein service include kar sakta. Category pehle required (consultation, laboratory etc.). Service fields name, auto/custom code, category, active, booking allowed, POS availability, duration minutes, price, tax rate, whether tax included or added, description, branches. Appointment service select karne ko booking checkbox chahiye. Billing-only lab/fee service bookable off ho sakti. No Tax/GST rate and tax-in-price policy should match local tax settings. Demo test category “DEMO TEST - Codex Review”, code COD-TEST, saved. Service “DEMO TEST - Consultation (Codex)”, Rs 1, booking on, save attempted; request returned HTTP 502, no service created. The service gap blocks appointment fee configuration.');
}

// 16 — Hospital overview
{
  const s = base('Hospital: patient se bill tak', 'Yeh intended workflow hai; demo setup incomplete hone ki wajah se aage ka hissa rukta hai', 16, { titleWidth: 1080 });
  addImage(s, 'hospital-journey-photo', img.hospital, 'Illustrative patient journey across reception, doctor, laboratory, pharmacy and billing', { left: 64, top: 174, width: 1144, height: 250 }, { fit: 'cover', geometry: 'roundRect', radius: 20 });
  const stages = ['Reception', 'Doctor / OPD', 'Lab', 'Pharmacy', 'Patient Billing'];
  stages.forEach((v, i) => {
    const x = 82 + i * 224;
    panel(s, `hospital-stage-${i}`, v, { left: x, top: 386, width: 202, height: 56 }, { fill: i === 0 ? C.mint : C.paper, stroke: C.teal, size: 19, bold: true });
    if (i < 4) arrow(s, `hospital-stage-arrow-${i}`, x + 205, 399, 25, C.teal);
  });
  panel(s, 'hospital-blocker-doctor', 'DOCTORS / SESSIONS CONFIGURED NAHIN', { left: 90, top: 492, width: 500, height: 58 }, { fill: C.paleRust, stroke: C.paleRust, color: C.rust, size: 19, bold: true });
  panel(s, 'hospital-blocker-service', 'SERVICE SAVE REQUEST: 502', { left: 690, top: 492, width: 500, height: 58 }, { fill: C.paleGold, stroke: C.paleGold, color: C.ink, size: 19, bold: true });
  text(s, 'hospital-dashboard-snapshot', 'Dashboard: test patient 1 • OPD 0 • inpatients 0 • beds 0/0\nPending lab 0 • prescriptions 0 • aaj invoice Rs. 0.00', { left: 92, top: 564, width: 1096, height: 58 }, { size: 17, color: C.ink2, align: 'center' });
  notes(s, 'Hospital Dashboard is read-only snapshot: registered patients, OPD waiting/with doctor/completed, current inpatients, admissions today, bed occupancy, pending lab, prescriptions and invoices today. Cards often link to worklists. Intended journey: first create billable services and doctor/sessions; Reception registers patient MRN and checks in; appointment or walk-in token enters OPD; fee cleared, doctor encounter records vitals/diagnosis/note and medication/lab order; pharmacy dispenses against stock; lab records results; Patient Billing aggregates services/payment. Demo: a synthetic patient registered today, OPD waiting 0, inpatients 0, admissions 0, beds 0/0, pending lab orders 0, prescriptions 0, today invoiced Rs 0.00. Doctor/session missing and service-save 502 are blockers; the clinical path was not proven end to end. Illustration is a generic explanatory image, not a screenshot of actual screen or hospital process evidence.');
}

// 17 — Patient/reception/appointments
{
  const s = base('Reception aur patient record', 'Patient ID (MRN) banta hai; visit/token tab shuru hota hai jab check-in ho', 17);
  const seq = [
    ['1', 'Register', 'Name • phone • gender • DOB ya approximate age'],
    ['2', 'MRN', 'System patient ko permanent ID deta hai'],
    ['3', 'Check-in', 'Save & Check In se visit / queue shuru'],
    ['4', 'Appointment', 'Doctor + available session chahiye'],
  ];
  seq.forEach((a, i) => {
    const y = 188 + i * 103;
    panel(s, `reception-num-${i}`, a[0], { left: 90, top: y, width: 52, height: 52 }, { fill: i === 3 ? C.paleRust : C.mint, stroke: 'none', size: 23, bold: true });
    text(s, `reception-title-${i}`, a[1], { left: 168, top: y + 1, width: 215, height: 35 }, { size: 24, bold: true });
    text(s, `reception-desc-${i}`, a[2], { left: 392, top: y + 1, width: 780, height: 50 }, { size: 19, color: C.ink2 });
    if (i < 3) line(s, `reception-connector-${i}`, 116, y + 56, 116, y + 102, C.teal, 3);
  });
  panel(s, 'patient-demo-outcome', '', { left: 100, top: 578, width: 1080, height: 64 }, { fill: C.ink, stroke: C.ink });
  text(s, 'patient-demo-outcome-copy', 'TEST RECORD: MRN-000001 • synthetic profile\nPhone/DOB/age/visit/history blank', { left: 124, top: 583, width: 1032, height: 54 }, { size: 19, bold: true, color: C.white, align: 'center', fit: 'none' });
  notes(s, 'Reception Counter walk-in New patient fields: full name, phone, gender, DOB or approximate age. MRN auto assigned. Save only patient master. Save & Check In starts visit/queue; Emergency flag may be available. Check-in ties to visit status, doctor/token and consultation fee collection. Reception Visits is live queue; date/status filters Booked, Waiting, With doctor, Completed, Cancelled/no-show; search name/MRN. Appointments book future slot in doctor session. Demo screen explicitly required doctors and sessions first. Doctor Schedules has Doctors, Calendar, Rooms. Doctor record full name, specialization, department, default consultation service, branch, active; login optional/linked later. Calendar sets session timing; Rooms defines clinic room schedule. Current branch had no doctor; default consultation service could not be selected because service save failed. Patient Registry permanent profile has demographics, guardian/emergency contact, medical history, files, visit history; these are sensitive. Safe synthetic test record saved: MRN-000001, gender Unknown, no DOB/age/phone, no visit/medical history/file. It remains in demo.');
}

// 18 — OPD/clinical
{
  const s = base('OPD aur Clinical Encounter', 'Queue ki fee clear ho to doctor ka visit note khulta hai', 18);
  const steps = [
    ['WORKLIST', 'Date • name/MRN • visit number • fee-cleared status', C.mint],
    ['ENCOUNTER', 'Vitals → diagnosis → clinical notes', C.paleGold],
    ['ORDERS', 'Medication • lab • follow-up', C.paleRust],
  ];
  steps.forEach((a, i) => {
    const x = 86 + i * 390;
    panel(s, `opd-step-${i}`, '', { left: x, top: 245, width: 310, height: 206 }, { fill: a[2], stroke: a[2] });
    text(s, `opd-step-tag-${i}`, a[0], { left: x + 22, top: 264, width: 266, height: 30 }, { size: 16, bold: true, color: C.teal, align: 'center' });
    text(s, `opd-step-body-${i}`, a[1], { left: x + 27, top: 312, width: 256, height: 104 }, { size: 21, bold: i === 1, align: 'center' });
    if (i < 2) arrow(s, `opd-arrow-${i}`, x + 320, 329, 50, C.teal);
  });
  text(s, 'opd-clinical-note', 'Order record hona ≠ dawa dispense hona. Pharmacy alag se lot/qty issue karti hai.', { left: 103, top: 510, width: 1060, height: 54 }, { size: 23, color: C.rust, bold: true, align: 'center' });
  panel(s, 'opd-test-state', '', { left: 106, top: 578, width: 1058, height: 64 }, { fill: C.ink, stroke: C.ink });
  text(s, 'opd-test-state-copy', 'DEMO: OPD queue 0\nKoi diagnosis, vitals, prescription, lab order ya clinical note create nahin hua', { left: 128, top: 583, width: 1014, height: 54 }, { size: 19, bold: true, color: C.white, align: 'center', fit: 'none' });
  notes(s, 'OPD Worklist shows checked-in patients, triage/visit number and doctor queue. Date filter, patient row, visit number, fee-cleared status. If fee clears, consultation can start. Demo queue empty thi. Clinical Encounters authorized doctor ka encounter: vitals, diagnosis, clinical notes, medication order, lab order, follow-up. Clinical facts should only be entered by authorized clinician and based on actual visit. Orders record hone ke baad bhi dispense/stock issue pharmacy workflow mein hota hai; lab order result release se alag hai. Demo mein encounter nahi banaya: no vitals, diagnosis, notes, prescription or lab test order. This presentation is not a medical how-to or a substitute for clinical protocols.');
}

// 19 — IPD/lab/pharmacy
{
  const s = base('IPD, Laboratory aur Hospital Pharmacy', 'Teen specialized workflows; har aik ko apna setup aur authorized staff chahiye', 19);
  const sections = [
    { x: 72, w: 353, title: 'IPD / BEDS', label: 'Ward → Room → Bed', body: 'Admission, transfer, observation, progress notes, discharge summary.', status: 'DEMO: beds 0/0', fill: C.mint },
    { x: 463, w: 353, title: 'LAB', label: 'Test → Sample → Result', body: 'Category, panel/test, specimen, numeric/text result, verify/release.', status: 'DEMO: no tests/orders', fill: C.paleGold },
    { x: 854, w: 353, title: 'PHARMACY', label: 'Rx → Batch → Dispense', body: 'Prescription verify, lot choose, qty issue; FEFO stock handling.', status: 'DEMO: no active Rx', fill: C.paleRust },
  ];
  sections.forEach((a, i) => {
    panel(s, `hospital-specialty-${i}`, '', { left: a.x, top: 196, width: a.w, height: 384 }, { fill: a.fill, stroke: a.fill });
    text(s, `hospital-specialty-title-${i}`, a.title, { left: a.x + 24, top: 219, width: a.w - 48, height: 34 }, { size: 17, bold: true, color: C.teal });
    text(s, `hospital-specialty-label-${i}`, a.label, { left: a.x + 22, top: 273, width: a.w - 44, height: 60 }, { size: 23, bold: true });
    text(s, `hospital-specialty-body-${i}`, a.body, { left: a.x + 22, top: 354, width: a.w - 44, height: 90 }, { size: 19, color: C.ink2, valign: 'top' });
    panel(s, `hospital-specialty-status-${i}`, a.status, { left: a.x + 24, top: 493, width: a.w - 48, height: 49 }, { fill: C.paper, stroke: C.paper, size: 17, bold: true, color: C.rust });
  });
  text(s, 'specialty-shared-note', 'Lab / medicine orders patient encounter se aate hain; Patient Billing un ke charges ko jor sakti hai.', { left: 88, top: 602, width: 1102, height: 34 }, { size: 18, color: C.ink2, align: 'center' });
  notes(s, 'IPD: hospital wards/rooms/beds must be configured first; admissions need authorized Hospital encounter, attending doctor and bed. Then transfers, observations, progress notes and discharge summary. Demo had no active ward/bed; occupancy 0/0. Lab: configure category, individual test/panel, code/name, shared billable service, specimen type, price, result type numeric/text. Orders come from authorized encounter and configured consent/emergency policy; results restricted/encrypted. Demo had no test catalog/order. Hospital Pharmacy verifies doctor prescription, selects branch stock batch, dispenses quantity and audits returns. FEFO = first expiry first out; use soonest-expiring lot first. Billing may be immediate payment or add to hospital bill; authorized pharmacy user can override per-dispense. No active prescription was present; no drug was dispensed. Lab results, clinical notes and pharmacy dispensing are restricted workflows.');
}

// 20 — Patient billing
{
  const s = base('Patient Billing aur OPD Collection', 'Patient account mein mukhtalif visits ke charges, receipts aur balance jurtay hain', 20);
  panel(s, 'billing-patient', 'PATIENT / MRN', { left: 468, top: 186, width: 340, height: 64 }, { fill: C.ink, stroke: C.ink, color: C.white, size: 24, bold: true });
  const charges = [
    ['Consultation', 111, C.mint], ['Laboratory', 328, C.paleGold], ['Procedure / treatment', 545, C.paleRust], ['Pharmacy', 762, C.mint],
  ];
  charges.forEach((a, i) => {
    panel(s, `bill-charge-${i}`, a[0], { left: a[1], top: 322, width: 190, height: 74 }, { fill: a[2], stroke: a[2], size: 19, bold: true });
    line(s, `bill-charge-stem-${i}`, a[1] + 95, 250, a[1] + 95, 322, C.teal, 3);
  });
  line(s, 'bill-balance-line', 206, 448, 1066, 448, C.teal, 4);
  panel(s, 'bill-receipts', 'Payments', { left: 266, top: 421, width: 190, height: 56 }, { fill: C.paleGold, stroke: C.paleGold, size: 18, bold: true });
  panel(s, 'bill-refunds', 'Refunds', { left: 533, top: 421, width: 190, height: 56 }, { fill: C.paleRust, stroke: C.paleRust, size: 18, bold: true });
  panel(s, 'bill-balance', 'Remaining balance', { left: 824, top: 421, width: 230, height: 56 }, { fill: C.mint, stroke: C.mint, size: 18, bold: true });
  panel(s, 'opd-collection-panel', 'OPD Collection = report/overview only\nSeen • waiting • consultation fee • discount • void/refund • doctor • payment method', { left: 147, top: 524, width: 986, height: 86 }, { fill: C.ink, stroke: C.ink, color: C.white, size: 20, bold: true });
  notes(s, 'Patient Billing searches name/MRN and branch access. Patient account groups multiple visits: consultation, lab, procedure, treatment, pharmacy invoices, payments, refunds, balances; check service-class line, due/paid. Reception can collect consultation; Patient Billing may include Procedure, Treatment, Other; lab often billed from test order. OPD Collection is a date-range read-only collection overview: seen/waiting/removed, consultation fees collected, discounts, void/refund, doctor/payment method. It does not create a charge/payment. Demo had no visits, so zero collection. No invoice/payment/refund was submitted during tests. Privacy: open patient data only with proper access; report did not include actual patient information.');
}

// 21 — Business configuration
{
  const s = base('Tools: business ki bunyadi settings', 'Pehle business structure, phir daily checkout', 21);
  const setup = [
    ['COMPANY', 'legal / display name\ntax / address'],
    ['BRANCH', 'business location\nbranch accounts'],
    ['STORE', 'warehouse / stock\nreceiving location'],
    ['REGISTER', 'POS till\nstore + cashier'],
  ];
  setup.forEach((a, i) => {
    const x = 82 + i * 292;
    panel(s, `setup-node-${i}`, '', { left: x, top: 206, width: 236, height: 164 }, { fill: i % 2 ? C.paleGold : C.mint, stroke: 'none' });
    text(s, `setup-title-${i}`, a[0], { left: x + 18, top: 228, width: 200, height: 32 }, { size: 17, bold: true, color: C.teal, align: 'center' });
    text(s, `setup-body-${i}`, a[1], { left: x + 20, top: 280, width: 196, height: 65 }, { size: 20, bold: true, align: 'center' });
    if (i < 3) arrow(s, `setup-arrow-${i}`, x + 244, 274, 44, C.teal);
  });
  const settings = [
    'Currency & date/number format', 'Tax • stock • reorder\nbatch rules', 'Pricing + receipts + shift/cash rules', 'Software branding + security', 'Industry profile + feature switches',
  ];
  settings.forEach((v, i) => {
    const x = 104 + i * 218;
    panel(s, `setup-setting-${i}`, v, { left: x, top: 456, width: 196, height: 104 }, { fill: C.paper, stroke: C.gray, size: 17, bold: i === 0 });
  });
  text(s, 'setup-note', 'Demo company ke private address/contact values guide mein share nahi kiye gaye.', { left: 110, top: 590, width: 1060, height: 34 }, { size: 18, color: C.ink2, align: 'center' });
  notes(s, 'Tools > Business Configuration includes Industry Profile (feature/workflow set), Company Profile (legal/display identity, tax registration, address/contact, document print identity; private demo address/contact omitted), Feature Management enable/disable modules, Branches, Stores/Warehouses, POS Registers. Currency & Formatting controls currency/decimal/date/number displays; Tax configures rates and invoice/service behavior; Inventory options reorder/valuation/batch/expiry; Pricing default/branch/customer rules; Receipt print layout; Shift & Cash register shift/float/opening-cash controls; Software Branding browser/sign-in/document logo/favicon/background; Security login/session. Correct hierarchy is company → branches → store/warehouse and cashbook/GL mappings → register/cashier. Before test checkout, ensure tax, price, receipt, store and register all align.');
}

// 22 — Admin, access, audit
{
  const s = base('Tools: users, permissions aur control', 'Har person ko sirf apne kaam jitni access dein', 22);
  const circleNodes = [
    { x: 88, title: 'ROLE', body: 'screens + actions', fill: C.mint },
    { x: 385, title: 'USER', body: 'login + branch/register', fill: C.paleGold },
    { x: 682, title: 'ACTION', body: 'sale / stock / voucher', fill: C.paleRust },
    { x: 979, title: 'AUDIT', body: 'who • what • when', fill: C.mint },
  ];
  circleNodes.forEach((a, i) => {
    panel(s, `access-node-${i}`, '', { left: a.x, top: 230, width: 202, height: 148 }, { fill: a.fill, stroke: a.fill });
    text(s, `access-title-${i}`, a.title, { left: a.x + 16, top: 251, width: 170, height: 34 }, { size: 20, bold: true, color: C.teal, align: 'center' });
    text(s, `access-body-${i}`, a.body, { left: a.x + 14, top: 303, width: 174, height: 45 }, { size: 18, color: C.ink2, align: 'center' });
    if (i < 3) arrow(s, `access-arrow-${i}`, a.x + 208, 287, 58, C.teal);
  });
  text(s, 'admin-tools-head', 'BAQI TOOLS', { left: 90, top: 440, width: 250, height: 26 }, { size: 16, color: C.teal, bold: true });
  text(s, 'admin-tools-body', 'Import/Export • Backup/Restore • Reason Codes • Audit Log • User Activity • Promotions • Price Books • Theme', { left: 90, top: 480, width: 1100, height: 70 }, { size: 22, bold: true, color: C.ink, align: 'center' });
  panel(s, 'admin-restore-caution', '', { left: 124, top: 568, width: 1030, height: 64 }, { fill: C.paleGold, stroke: C.paleGold });
  text(s, 'admin-restore-caution-copy', 'Import se pehle duplicate, codes aur UOM verify karein.\nRestore current data replace kar sakta hai.', { left: 144, top: 574, width: 990, height: 52 }, { size: 19, bold: true, color: C.ink, align: 'center', fit: 'none' });
  notes(s, 'Users profile/login/password, role, assigned branches/registers and extra rights. Roles & Rights built-in/custom role screen/action permissions. Import/Export Products, customers, suppliers Excel/CSV; validate column names, codes, UOM and duplicates before importing. Backup/Restore portable client database snapshot download/restore; restore can replace current data so verify file and target. Audit Log is server read-only audited trail for money/stock/security actions. User Activity per-user login history, breakdown, recent events. Reason Codes standard reason for void/refund/override/stock adjustment. Promotions POS automatic percent/fixed/Buy-X-Get-Y rules. Price Books customer/group/branch/default base selling price; server resolves most applicable price then promotion applies. Theme changes application appearance; demo multiple themes and High Contrast. Access, audit, restore and print actions can impact data or external output; assign only authorized operators and validate target before irreversible actions.');
}

// 23 — Demo evidence
{
  const s = base('Maine demo mein kya test kiya?', 'Actual test result aur system ka intended behavior alag rakha hai', 23, { dark: true });
  const facts = [
    ['PASS', 'Patient save', 'Synthetic patient → MRN-000001'],
    ['PASS', 'Category save', 'DEMO TEST - Codex Review'],
    ['BLOCK', 'Service save', 'Backend 502 → service nahin bani'],
    ['DRAFT', 'POS lookup', 'SKU 0001 • Rs. 200 • Rs. 300 change'],
    ['DRAFT', 'Journal', 'Rs. 1 = Rs. 1 → Balanced'],
  ];
  facts.forEach((a, i) => {
    const y = 172 + i * 78;
    const fill = i === 2 ? C.rust : (i < 2 ? C.teal : C.ink2);
    panel(s, `evidence-status-${i}`, a[0], { left: 78, top: y, width: 122, height: 48 }, { fill, stroke: fill, color: C.white, size: 16, bold: true });
    text(s, `evidence-test-${i}`, a[1], { left: 226, top: y + 1, width: 220, height: 42 }, { size: 21, bold: true, color: C.white });
    text(s, `evidence-desc-${i}`, a[2], { left: 458, top: y + 1, width: 700, height: 42 }, { size: 19, color: '#D2E1DE' });
  });
  panel(s, 'evidence-note', 'Koi payment, sale charge, return, stock post, purchase, voucher post, clinical note, lab, dawa ya print submit nahin ki.', { left: 84, top: 578, width: 1112, height: 58 }, { fill: C.gold, stroke: C.gold, color: C.navy, size: 18, bold: true });
  notes(s, 'Test evidence: Patient Registry synthetic record save successful: MRN-000001, gender Unknown, DOB/age/phone blank, no visit/history/files. Service Catalog test category DEMO TEST - Codex Review, code COD-TEST saved and remains. Attempted billable service DEMO TEST - Consultation (Codex), Rs 1, booking on; save returned 502; no service created. POS name search “Antibiotics” returned no result; SKU 0001 located Rs 200 item, stock 199. Draft qty 1, amount received Rs 500; screen showed Rs 300 change due; line removed; no Charge, sale or stock post. Journal draft Rs 1 Expense debit / Rs 1 Sales credit showed Balanced and Post enabled; backed out; no voucher posted. Purchase Order New form reviewed/canceled; no order saved. Appointment page required doctor/session; none configured. Hospital snapshot: 1 test patient today; OPD waiting 0, inpatients 0, admissions 0, beds 0/0, lab 0, prescriptions 0, today invoiced Rs 0.00. Non-submitted actions: no payment, invoice charge, return/refund, stock receipt/adjustment, purchase invoice, voucher posting, printing, clinical encounter, diagnosis, medicine order. Existing saved test records on demo: test service category and synthetic patient.');
}

// 24 — Go-live sequence
{
  const s = base('Go-live se pehle sahi tarteeb', 'Prerequisites pehle tayyar hon, phir daily transaction test karein', 24);
  const stages = [
    { x: 78, w: 212, n: '01', title: 'Company', body: 'Branch • store\nregister • tax' },
    { x: 310, w: 212, n: '02', title: 'Accounts', body: 'Chart • cash/bank\nrevenue mapping' },
    { x: 542, w: 212, n: '03', title: 'Catalog', body: 'Products • UOM\nprices • opening stock' },
    { x: 774, w: 212, n: '04', title: 'Retail test', body: 'Supplier • customer\nshift • SKU sale' },
    { x: 1006, w: 212, n: '05', title: 'Hospital', body: 'Services • doctor\nsession • rooms/beds' },
  ];
  stages.forEach((a, i) => {
    panel(s, `golive-stage-${i}`, '', { left: a.x, top: 215, width: a.w, height: 186 }, { fill: i === 4 ? C.paleRust : (i % 2 ? C.paleGold : C.mint), stroke: 'none' });
    text(s, `golive-num-${i}`, a.n, { left: a.x + 18, top: 231, width: 56, height: 34 }, { size: 20, bold: true, color: C.teal });
    text(s, `golive-title-${i}`, a.title, { left: a.x + 18, top: 281, width: a.w - 36, height: 34 }, { size: 23, bold: true });
    text(s, `golive-body-${i}`, a.body, { left: a.x + 18, top: 329, width: a.w - 36, height: 58 }, { size: 17, color: C.ink2 });
    if (i < 4) arrow(s, `golive-arrow-${i}`, a.x + 214, 284, 18, C.teal);
  });
  text(s, 'golive-step2', 'Phir OPD check-in → fee → encounter → lab/pharmacy → Patient Billing → reports/audit/shift close/backups', { left: 94, top: 461, width: 1092, height: 66 }, { size: 21, bold: true, color: C.ink, align: 'center' });
  panel(s, 'golive-caveat', 'Blocker: service save par 502; doctor/session abhi configured nahin.', { left: 178, top: 558, width: 924, height: 52 }, { fill: C.paleRust, stroke: C.paleRust, color: C.rust, size: 19, bold: true });
  notes(s, 'Suggested initial setup sequence from the demo guide: (1) Company/industry, branch, store/warehouse, POS register, currency/tax. (2) Accounts Group, Chart, branch cash/bank/sales mapping, Revenue Accounts, fiscal periods. (3) Product groups/categories/UOM/brands/products/prices/per-store opening stock. (4) Supplier/customer masters and price books. (5) POS shift, receipt, cash account, draft SKU/barcode test then authorized post. (6) Hospital service categories/billable services; resolve/retry the 502 issue. (7) Doctors, departments, branches, rooms and OPD sessions; attach default consultation service. (8) Patient registration, Reception check-in, fee clearance, worklist test. (9) As needed lab categories/tests/specimens/consent-emergency policy; IPD wards/rooms/beds; pharmacy stock. (10) Authorized test encounter, lab result, pharmacy dispense, Patient Billing. (11) Reports, audit log, shift close, bank reconciliation, backup schedule and permission review. Test in a safe environment with synthetic data and separate proof from production data.');
}

// 25 — Glossary and close
{
  const s = base('Chhoti glossary — system ki zaban', 'In lafzon ko samajh lein to menus aur reports asan ho jate hain', 25);
  const terms = [
    ['SKU', 'item code'], ['UOM', 'maap / unit'], ['Batch / Lot', 'stock ki aik khep'], ['Reorder', 'dobara mangwane ki had'],
    ['MRN', 'patient record ID'], ['OPD', 'bila admission doctor visit'], ['IPD', 'ward mein admission'], ['FEFO', 'pehle expiry, pehle issue'],
    ['AR', 'customer se lena'], ['AP', 'supplier ko dena'], ['GL / Ledger', 'accounting khata'], ['Debit / Credit', 'double-entry ki do sides'],
  ];
  terms.forEach((a, i) => {
    const row = Math.floor(i / 4);
    const col = i % 4;
    const x = 74 + col * 294;
    const y = 184 + row * 122;
    text(s, `glossary-term-${i}`, a[0], { left: x, top: y, width: 260, height: 36 }, { size: 20, bold: true, color: C.teal, align: 'center' });
    panel(s, `glossary-def-${i}`, a[1], { left: x + 16, top: y + 43, width: 232, height: 52 }, { fill: row % 2 ? C.paleGold : C.mint, stroke: 'none', size: 18, color: C.ink, bold: true });
  });
  text(s, 'draft-posted-definition', 'Draft / Held = abhi final nahin     •     Posted = stock/accounts/reports mein record ho chuka', { left: 88, top: 571, width: 1104, height: 34 }, { size: 18, bold: true, color: C.rust, align: 'center' });
  text(s, 'end-message', 'Pehle setup. Phir draft check. Sirf authorized final button se post.', { left: 128, top: 616, width: 1024, height: 32 }, { size: 20, bold: true, color: C.ink, align: 'center' });
  notes(s, 'Glossary: SKU = product internal code; UOM = Unit of Measure (piece/box/kg/litre); Batch/Lot = same product stock grouped by cost and expiry; Reorder level = minimum quantity alert; MRN = Medical Record Number; OPD = outpatient visit without admission; IPD = inpatient ward/bed admission; FEFO = First Expiry First Out; AR/Receivable = customer amount owed to us; AP/Payable = amount we owe supplier; GL = General Ledger; Debit/Credit = double-entry sides whose journal totals should match. Posted = transaction finalized and recorded in stock/accounts/reporting. Draft/Held = not final; draft can be edited and held POS bill can be reopened. Void cancels an invoice; refund returns money; return can reintroduce item/credit depending on workflow. Check reason/audit trail for each. Closing summary: demo retail screens show navigation/forms; POS SKU lookup and change math were demonstrated as a draft; journal balance validation worked as a draft. Patient synthetic record was registered. Hospital end-to-end workflow could not be proved in this demo because there were no doctors/sessions and service save returned 502.');
}

// Snapshot authoring diagnostics and export a reviewable draft.
await fs.mkdir(path.join(HERE, 'rendered'), { recursive: true });
const slideSummaries = [];
for (const [i, slide] of deck.slides.items.entries()) {
  const file = path.join(HERE, 'rendered', `slide-${String(i + 1).padStart(2, '0')}.png`);
  const png = await deck.export({ slide, format: 'png', scale: 1 });
  await fs.writeFile(file, new Uint8Array(await png.arrayBuffer()));
  slideSummaries.push({ slide: i + 1, file, notes: (slide.speakerNotes.text || '').length });
}
const montage = await deck.export({ format: 'webp', montage: true, scale: 0.3 });
await fs.writeFile(path.join(HERE, 'rendered', 'montage.webp'), new Uint8Array(await montage.arrayBuffer()));
await fs.writeFile(path.join(HERE, 'slide-summary.json'), JSON.stringify({ count: deck.slides.items.length, slides: slideSummaries }, null, 2));

const draftPath = path.join(HERE, 'candidate.pptx');
const draft = await PresentationFile.exportPptx(deck);
await draft.save(draftPath);

// Validate and write the separately named final artifact.
const { finalizePresentation, resolvePresentationFont } = await import(pathToFileURL(path.join(SKILL_DIR, 'container_tools', 'artifact_tool_utils.mjs')).href);
const chosenFont = resolvePresentationFont({ fontFamily: FONT });
const finalPath = path.join(WORKSPACE, 'presentation_output', 'InfraSpine_HMS_Roman_Urdu_Visual_Guide_Final_v3.pptx');
const validationDir = path.join(HERE, 'validation');
await fs.mkdir(validationDir, { recursive: true });
const result = await finalizePresentation({
  explicitTotalSlideCount: deck.slides.items.length,
  workspaceDir: WORKSPACE,
  candidatePath: draftPath,
  finalPath,
  pythonExecutable: 'C:\\Program Files\\WindowsApps\\OpenAI.CodexPrimaryRuntime.v26-1112-737-0_26.1112.737.0_x64__3k8sg7r9htsxt\\dependencies\\python\\python.exe',
  integrityValidatorPath: path.join(SKILL_DIR, 'container_tools', 'inspect_presentation_package_integrity.py'),
  layoutValidatorPath: path.join(SKILL_DIR, 'container_tools', 'inspect_presentation_layout_geometry.py'),
  layoutArgs: ['--expected-slide-size-emu', '12192000,6858000', '--validate-bullet-geometry', '--validate-heading-fit'],
  fontPolicy: { basis: 'design', families: [chosenFont] },
  verifyArtifactToolImport: true,
  receiptPath: path.join(validationDir, 'validation-final-v3.json'),
});
console.log(JSON.stringify({ finalPath, slideCount: deck.slides.items.length, font: chosenFont, result }, null, 2));
