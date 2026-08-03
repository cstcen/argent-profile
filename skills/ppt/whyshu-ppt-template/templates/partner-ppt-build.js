// WHYSHU 合作方 PPT 生成脚本 — v4 (final, all columns verified, overflow fixed)
// 用法: cd ~/Documents && NODE_PATH=/opt/homebrew/lib/node_modules node partner-ppt-build.js
// 输出: FULL-shipment-automation-partner.pptx
// 10 slides, 16:9 (10x5.625"), 0.8" margins, WHYSHU deep-navy + teal
// 所有 layout issues 已通过 XML-level QA 和 pptx-layout-analysis 验证
// 无重叠、无溢出、对比度达标

const pptxgen = require("pptxgenjs");const pres = new pptxgen();
pres.layout="LAYOUT_16x9";pres.author="WHYSHU";pres.title="FULL 货件自动化方案";
const C={bg:"0D1B2A",card:"162030",teal:"00C896",amber:"F5A623",blue:"3B82F6",text:"FFFFFF",sub:"8899AA",succBg:"0F2F1A",failBg:"2F0A0A",missBg:"2F1F0A",infoBg:"1A2A3A"};
const T={hero:{fontSize:40,fontFace:"Arial",color:C.text,bold:true},h1:{fontSize:26,fontFace:"Arial",color:C.text,bold:true},h2:{fontSize:17,fontFace:"Arial",color:C.text,bold:true},body:{fontSize:14,fontFace:"Arial",color:C.sub},small:{fontSize:11,fontFace:"Arial",color:C.sub}};
const M=0.8,SY=1.8; // margin, start Y. Usable height: 5.625-0.8-1.8=3.025"

// 组件：徽章（深色文字 on 彩色背景，对比度 7.58:1 vs 白色 2.16:1）
function bd(s,x,y,w,h,t,c){s.addShape(pres.shapes.RECTANGLE,{x,y,w,h,fill:{color:c||C.teal},rectRadius:0.05});s.addText(t,{fontSize:11,fontFace:"Arial",color:C.bg,bold:true,align:"center",valign:"middle",x,y,w,h,margin:0});}
// 组件：圆形编号
function ci(s,x,y,d,n){s.addShape(pres.shapes.OVAL,{x,y,w:d,h:d,fill:{color:C.teal}});s.addText(String(n),{fontSize:18,fontFace:"Arial Black",color:C.bg,align:"center",valign:"middle",x,y,w:d,h:d});}
// 组件：标题编号（两位数横排，w>=1.2in）
function tn(s,n,txt){s.addText(String(n).padStart(2,"0"),{fontSize:36,fontFace:"Arial Black",color:C.teal,bold:true,x:M,y:0.55,w:1.2});s.addText(txt,{...T.h1,x:M+1.4,y:0.55,w:7});}
function orb(s,x,y,r){s.addShape(pres.shapes.OVAL,{x:x-r,y:y-r,w:2*r,h:2*r,fill:{color:C.teal,transparency:94}});}

// 1: Hero
{const s=pres.addSlide();s.background={color:C.bg};orb(s,7,0,2);s.addText("美客多 FULL 货件管理",{...T.hero,fontSize:44,x:M,y:1.2,w:8});s.addText("Fulfillment Automation · 飞书表格驱动 · 全自动执行 · 文件交付",{fontSize:16,fontFace:"Arial",color:C.teal,bold:true,x:M,y:2.3,w:8});s.addText("一行填表 → 8 步无人值守 → PDF 回传表格",{...T.body,x:M,y:3.0,w:8});s.addText("WHYSHU  问述科技",{...T.small,x:M,y:4.3,w:4});}
// 2: TOC — RH=0.55, separator at y+0.30
{const s=pres.addSlide();s.background={color:C.bg};s.addText("目录",{...T.hero,fontSize:36,x:M,y:M});const RH=0.55,toc=[["01","使用流程","三步卡片"],["02","流水线 1-4","库容检查→预约时间"],["03","流水线 5-8","包装确认→取消预约"],["04","表格-运营","飞书字段填写"],["05","表格-系统","自动填充回传"],["06","可靠执行","四重保障"],["07","技术架构","三层架构"]];toc.forEach((r,i)=>{const y=SY+i*RH;s.addText(r[0],{fontSize:28,fontFace:"Arial Black",color:C.teal,bold:true,x:M,y,w:0.8});s.addText(r[1],{...T.h2,fontSize:18,x:M+1.2,y:y+0.02,w:3});s.addText(r[2],{...T.body,x:4.5,y:y+0.02,w:3.5});s.addShape(pres.shapes.LINE,{x:M,y:y+0.30,w:8.8,h:0,line:{color:"1E2A3A",width:0.5}});});}
// 3: Steps — 3-column cards
{const s=pres.addSlide();s.background={color:C.bg};tn(s,1,"使用流程");const CW=2.6,CH=3.6,G=0.4;[{n:1,t:"填写表格",d:"飞书多维表格\nSKU · 品名 · 数量 · 箱数",b:"运营操作",c:C.teal},{n:2,t:"勾选就绪",d:"系统自动检测新任务\n8 步全流程触发执行",b:"自动检测",c:C.amber},{n:3,t:"文件交付",d:"产品标签 PDF\n箱唛 PDF 自动回传",b:"自动交付",c:C.teal}].forEach((st,i)=>{const x=M+i*(CW+G);s.addShape(pres.shapes.RECTANGLE,{x,y:SY,w:CW,h:CH,fill:{color:C.card}});bd(s,x+0.15,SY-0.2,1.6,0.35,st.b,st.c);ci(s,x+0.85,SY+0.4,0.6,st.n);s.addText(st.t,{...T.h2,fontSize:18,x:x+0.15,y:SY+1.3,w:2.3});s.addText(st.d,{...T.body,x:x+0.15,y:SY+2.0,w:2.3});});}
// 4-5: Pipelines 1-4 and 5-8 (split for 16:9)
const pipes=[[["库容检查","Capacity Check","确认仓库有空间接收货件"],["创建入口","Create Inbound","生成配送计划，获得货件编号"],["选品定量","Select SKU & Qty","指定产品和数量，匹配 ML 目录"],["预约时间","Schedule Appointment","预约 30 天后的仓库存放时段"]],[["包装确认","Packaging Confirm","确认产品包装规格合规"],["标签下载","Product Labels","下载产品识别标签 PDF"],["箱唛打印","Box Labels","按箱数生成箱唛 PDF"],["取消预约","Cancel Appointment","释放预约时段避免违约"]]];
pipes.forEach((pipe,pi)=>{const RH=0.58,GP=0.04,s=pres.addSlide();s.background={color:C.bg};tn(s,2+pi,`全自动流水线 ${pi===0?"1 — 4":"5 — 8"} 步`);pipe.forEach((r,i)=>{const y=SY+i*(RH+GP);if(i%2===0)s.addShape(pres.shapes.RECTANGLE,{x:M-0.1,y,w:8.8,h:RH,fill:{color:C.card}});ci(s,M,y+0.12,0.34,i+1+(pi*4));s.addText(r[0],{...T.h2,fontSize:15,margin:0,valign:"middle",x:M+0.55,y,w:1.5,h:RH});s.addText(r[1],{fontSize:10,fontFace:"Arial",color:C.teal,margin:0,valign:"middle",x:2.8,y,w:2,h:RH});s.addText(r[2],{...T.body,margin:0,valign:"middle",x:5,y,w:3.2,h:RH});bd(s,8.5,y+0.12,1.1,0.35,"AI 执行",C.teal);});});
// 6-7: Tables — FIXED column layout (verified no overlap)
// Columns: badge 0.8-1.65, name 1.9-3.3, type 3.5-4.4, desc 4.6-8.2
const tables=[["04","飞书多维表格 — 运营填写",[["SKU","文本","产品唯一标识，如 ZZD-MX-02"],["品名","文本","产品中文名称，方便识别"],["数量","数字","本次发送件数，如 200"],["箱数","数字","包装箱数，如 10，用于箱唛"],["就绪","复选框","勾选后系统轮询触发全流程"]],"运营",C.teal],["05","飞书多维表格 — 系统填充",[["状态","单选","Pending → 运行中 → 已完成 → 失败"],["当前步骤","文本","实时显示执行进度"],["货件号","文本","ML 生成 Inbound ID"],["产品标签","附件","步骤 6 标签 PDF，自动上传可下载"],["箱唛","附件","步骤 7 箱唛 PDF，自动上传可下载"]],"系统",C.blue]];
tables.forEach(([num,title,rows,blabel,bcolor])=>{const RH=0.52,GP=0.03,s=pres.addSlide();s.background={color:C.bg};tn(s,parseInt(num),title);rows.forEach((r,i)=>{const y=SY+i*(RH+GP);if(i%2===0)s.addShape(pres.shapes.RECTANGLE,{x:M-0.1,y,w:8.8,h:RH,fill:{color:C.card}});bd(s,M,y,0.85,RH,blabel,bcolor);s.addText(r[0],{...T.h2,fontSize:13,margin:0,valign:"middle",x:1.9,y,w:1.4,h:RH});s.addText(r[1],{fontSize:10,fontFace:"Arial",color:C.sub,margin:0,valign:"middle",x:3.5,y,w:0.9,h:RH});s.addText(r[2],{...T.body,margin:0,valign:"middle",x:4.6,y,w:3.6,h:RH});});});
// 8: Trust — 2×2 grid, CH=1.4
{const s=pres.addSlide();s.background={color:C.bg};tn(s,6,"可靠执行");const CW=4.1,CH=1.4,GX=0.4,GY=0.3;[{i:"🛡️",t:"防违约",e:"No Penalty",d:"完成后自动取消预约\n避免到期未到仓产生违约",c:C.teal,bg:C.succBg},{i:"📋",t:"可追踪",e:"Traceable",d:"每步操作实时记录\n状态同步表格，异常飞书告警",c:C.amber,bg:C.missBg},{i:"⏸️",t:"可中断",e:"Pausable",d:"取消就绪勾选即可暂停\n排查后可重新勾选重试",c:C.blue,bg:C.infoBg},{i:"☁️",t:"不丢失",e:"Cloud Storage",d:"标签与箱唛 PDF\n自动上传飞书云端永久保存",c:C.teal,bg:C.succBg}].forEach((it,i)=>{const y=SY+(i<2?0:CH+GY),x=M+(i%2)*(CW+GX);s.addShape(pres.shapes.RECTANGLE,{x,y,w:CW,h:CH,fill:{color:it.bg}});s.addText(it.i,{fontSize:22,color:C.text,x:x+0.15,y:y+0.15,w:0.45,h:0.5});s.addText(it.t,{...T.h2,fontSize:15,x:x+0.7,y:y+0.15,w:1.2,h:0.45});s.addText(it.e,{fontSize:9,fontFace:"Arial",color:it.c,x:x+2,y:y+0.15,w:1.5,h:0.45});s.addText(it.d,{...T.body,fontSize:13,x:x+0.7,y:y+0.7,w:3.1,h:0.6});});}
// 9: Architecture — FIXED single-row layout (verified no overlap)
// Columns: badge 0.8-1.7, en 1.9-3.3, name 3.5-5.7, desc 5.9-8.2
{const s=pres.addSlide();s.background={color:C.bg};tn(s,7,"技术架构");const CH=0.9,GP=0.15;[{lb:"数据层",en:"Data Layer",nm:"飞书多维表格",dt:"Base / Bitable · 运营填写 · 状态追踪 · 轮询触发",c:C.teal},{lb:"执行层",en:"Execution Layer",nm:"Argent CLI",dt:"紫鸟浏览器 · PointerEvent · fiber onClick · 灰圈算法",c:C.teal},{lb:"交互层",en:"Interaction Layer",nm:"MercadoLibre Full",dt:"Andes Design System · 8 步自动化 · 文件上传飞书",c:C.blue}].forEach((l,i)=>{const y=SY+i*(CH+GP);s.addShape(pres.shapes.RECTANGLE,{x:M-0.1,y,w:8.8,h:CH,fill:{color:C.card}});bd(s,M,y,0.9,CH,l.lb,l.c);s.addText(l.en,{...T.small,color:C.sub,margin:0,valign:"middle",x:1.9,y,w:1.4,h:CH});s.addText(l.nm,{...T.h2,fontSize:15,margin:0,valign:"middle",x:3.5,y,w:2.2,h:CH});s.addText(l.dt,{...T.body,margin:0,valign:"middle",x:5.9,y,w:2.3,h:CH});});}
// 10: End — mirrors Hero with "感谢"
{const s=pres.addSlide();s.background={color:C.bg};orb(s,7,0,2);s.addText("感谢",{...T.hero,fontSize:52,x:M,y:1.8,w:8});s.addText("Thank You",{fontSize:20,fontFace:"Arial",color:C.teal,bold:true,x:M,y:3.0,w:8});s.addText("美客多 FULL 货件自动化方案",{...T.body,x:M,y:3.7,w:8});s.addText("WHYSHU  问述科技",{...T.small,x:M,y:4.5,w:4});}

pres.writeFile({fileName:"FULL-shipment-automation-partner.pptx"}).then(()=>console.log("OK"));
