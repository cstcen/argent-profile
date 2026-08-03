#!/bin/bash
# Argent FULL 货件自动化 — 飞书多维表格驱动
# 轮询逻辑：检测 状态=Pending && 就绪=true → 自动执行全流程
# 使用前请通过环境变量配置（见 .env.example 或各变量注释）

# ── 必填：运行前配置（勿提交真实值到版本库）──
BASE_TOKEN="${FEISHU_BASE_TOKEN:?请设置环境变量 FEISHU_BASE_TOKEN（飞书多维表格 Base Token）}"
TABLE_ID="${FEISHU_TABLE_ID:?请设置环境变量 FEISHU_TABLE_ID}"
STORE_ID="${ML_STORE_ID:?请设置环境变量 ML_STORE_ID}"
STORE_NAME="${ML_STORE_NAME:?请设置环境变量 ML_STORE_NAME}"
FEISHU_USER="${FEISHU_USER_ID:?请设置环境变量 FEISHU_USER_ID}"
ZINIAO_DL="${ZINIAO_DL:-$HOME/Library/Application Support/ziniaobrowserdatas/ziniao browser}"

# ── 工具函数 ──
update_field() {
  local record_id=$1 field=$2 value=$3
  lark-cli base +record-batch-update \
    --base-token "$BASE_TOKEN" --table-id "$TABLE_ID" \
    --record-ids "$record_id" \
    --json "{\"fields\":{\"$field\":$value}}" 2>/dev/null
}

update_step() {
  local record_id=$1 step=$2
  update_field "$record_id" "当前步骤" "\"$step\""
}

push_feishu() {
  local msg=$1
  lark-cli im +messages-send --user-id "$FEISHU_USER" --text "$msg" 2>/dev/null
}

upload_file() {
  local record_id=$1 field=$2 file=$3
  cd "$ZINIAO_DL"
  lark-cli base +record-upload-attachment \
    --base-token "$BASE_TOKEN" --table-id "$TABLE_ID" \
    --record-id "$record_id" --field-id "$field" --file "$file" 2>/dev/null
}

# ── 主循环 ──
echo "🔍 Argent FULL 轮询启动..."
push_feishu "🔍 Argent FULL 货件轮询已启动"

while true; do
  # 查询 Pending 且就绪的记录
  echo "📋 轮询..."
  RECORDS=$(lark-cli base +record-list \
    --base-token "$BASE_TOKEN" --table-id "$TABLE_ID" \
    --filter 'CurrentValue.[状态]="Pending"' 2>/dev/null | \
    python3 -c "
import sys,json
try:
    d=json.load(sys.stdin)
    for r in d['data']['items']:
        f=r['fields']
        if f.get('就绪')==True:
            print(r['_record_id'])
            print(f.get('SKU',''))
            print(f.get('品名',''))
            print(str(f.get('数量','')))
            print(str(f.get('箱数','')))
            break
except: pass
")

  if [ -z "$RECORDS" ]; then
    sleep 30
    continue
  fi

  # 解析记录
  RECORD_ID=$(echo "$RECORDS" | head -1)
  SKU=$(echo "$RECORDS" | head -2 | tail -1)
  NAME=$(echo "$RECORDS" | head -3 | tail -1)
  QTY=$(echo "$RECORDS" | head -4 | tail -1)
  BOX=$(echo "$RECORDS" | head -5 | tail -1)

  echo "🚀 开始处理: $SKU $NAME ${QTY}件 ${BOX}箱"
  push_feishu "🚀 FULL 货件启动: $SKU $NAME ${QTY}件 ${BOX}箱"
  update_field "$RECORD_ID" "状态" '"运行中"'
  update_step "$RECORD_ID" "步骤1：打开店铺"

  # ── 步骤1-3：前期准备+选品 ──
  ziniao-cli store open --name "$STORE_NAME" --headless 2>/dev/null
  ziniao-cli page visit --store-id "$STORE_ID" \
    --url "https://myaccount.mercadolibre.com.mx/shipping/inbounds" --wait-until networkidle 2>/dev/null

  # 点击 Enviar productos
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var l=document.querySelectorAll('a.andes-button');for(var i=0;i<l.length;i++){if(l[i].textContent.trim()==='Enviar productos'){l[i].click();break;}}return'ok';})();
  " 2>/dev/null

  sleep 5

  # 搜索SKU
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var i=document.querySelector('input[placeholder*=\"搜索\"]');if(!i)return;var ns=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;ns.call(i,'$SKU');i.dispatchEvent(new Event('input',{bubbles:true}));i.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));return'searched';})();
  " 2>/dev/null

  sleep 3

  # 填数量
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var inputs=document.querySelectorAll('input[id^=\"_r_\"]');var q=null;inputs.forEach(function(x){if(!x.placeholder&&x.getBoundingClientRect().top>500)q=x;});if(q){q.click();q.focus();var ns=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;ns.call(q,'$QTY');q.dispatchEvent(new Event('input',{bubbles:true}));}return'ok';})();
  " 2>/dev/null

  # 点击 Continuar
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var b=document.querySelectorAll('button');for(var i=0;i<b.length;i++){if(b[i].textContent.trim()==='Continuar'||b[i].textContent.trim()==='继续'){b[i].click();break;}}return'ok';})();
  " 2>/dev/null

  sleep 4

  # 弹窗关闭
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var b=document.querySelectorAll('button');for(var i=0;i<b.length;i++){if(b[i].textContent.includes('Mantener')||b[i].textContent.includes('维持')||b[i].textContent.includes('seguir')){b[i].click();break;}}return'ok';})();
  " 2>/dev/null

  sleep 3

  # 获取货件号
  SHIPMENT=$(ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var m=location.href.match(/inbounds\\/(\d+)/);return m?m[1]:'unknown';})();
  " 2>/dev/null | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['data']['result'])" 2>/dev/null)

  update_field "$RECORD_ID" "货件号" "\"$SHIPMENT\""
  update_step "$RECORD_ID" "步骤4：预约时间"

  push_feishu "✅ 步骤1-3完成: 货件 #$SHIPMENT $SKU ${QTY}件"

  # ── 步骤4：预约 ──
  # 进入预约页
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){
    var c=document.querySelector('[role=combobox]');var r=c.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;
    c.dispatchEvent(new PointerEvent('pointerover',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse'}));
    c.dispatchEvent(new PointerEvent('pointerenter',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse'}));
    c.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:1}));
    c.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:0}));
    c.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,view:window}));
    return'ok';
  })();" 2>/dev/null
  sleep 1
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var o=document.querySelectorAll('[role=option]');for(var i=0;i<o.length;i++){if(o[i].textContent.includes('Vehículo')){o[i].click();break;}}return'ok';})();
  " 2>/dev/null
  sleep 1
  # 开日历
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var i=document.querySelector('input[readonly][id^=\"_R_\"]');var r=i.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;i.dispatchEvent(new PointerEvent('pointerover',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse'}));i.dispatchEvent(new PointerEvent('pointerenter',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse'}));i.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:1}));i.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:0}));i.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,view:window}));return'ok';})();
  " 2>/dev/null
  sleep 1
  # 翻月
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var n=document.querySelector('[aria-label=\"next month\"]');var r=n.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;n.dispatchEvent(new PointerEvent('pointerover',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse'}));n.dispatchEvent(new PointerEvent('pointerenter',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse'}));n.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:1}));n.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:0}));n.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,view:window}));return'ok';})();
  " 2>/dev/null
  sleep 1
  # 选日+选时+确认
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var g=document.querySelector('div.day--current');var a=Array.from(document.querySelectorAll('div.day'));var t=a.indexOf(g)+31;var d=a[t];var r=d.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;d.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:1}));d.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:0}));d.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,view:window}));return'd='+d.textContent;})();
  " 2>/dev/null
  sleep 1
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var h=document.querySelector('div.hour');var r=h.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2;h.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:1}));h.dispatchEvent(new PointerEvent('pointerup',{bubbles:true,clientX:x,clientY:y,pointerId:1,pointerType:'mouse',button:0,buttons:0}));h.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,clientX:x,clientY:y,button:0,view:window}));return'ok';})();
  " 2>/dev/null
  sleep 1
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var b=document.querySelectorAll('button');for(var i=0;i<b.length;i++){if(b[i].textContent.trim()==='Confirmar'&&!b[i].disabled){b[i].click();}}return'ok';})();
  " 2>/dev/null
  sleep 5

  update_step "$RECORD_ID" "步骤5-6：包装+标签"
  push_feishu "✅ 步骤4完成: #$SHIPMENT"

  # ── 步骤5-6：包装确认+标签下载 ──
  # 包装
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var c=document.querySelectorAll('[role=button]');for(var i=0;i<c.length;i++){if(c[i].textContent.includes('Revisa')){c[i].click();break;}}return'ok';})();
  " 2>/dev/null
  sleep 2
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var cbs=document.querySelectorAll('input[type=checkbox]');cbs.forEach(function(cb){cb.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));});var b=document.querySelectorAll('button');for(var i=0;i<b.length;i++){if(b[i].textContent.trim()==='Confirmar'){b[i].click();break;}}return'ok';})();
  " 2>/dev/null
  sleep 3

  # 标签
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var c=document.querySelectorAll('[role=button]');for(var i=0;i<c.length;i++){if(c[i].textContent.includes('etiquetas')&&!c[i].textContent.includes('bultos')){c[i].click();break;}}return'ok';})();
  " 2>/dev/null
  sleep 3
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var b=document.querySelectorAll('button');for(var i=0;i<b.length;i++){if(b[i].textContent.trim()==='Producto'){b[i].click();break;}}document.querySelectorAll('[data-andes-checkbox-container]').forEach(function(c){var k=Object.keys(c).find(function(x){return x.startsWith('__reactFiber')});if(!k)return;var n=c[k];for(var j=0;j<15&&n;j++){if(n.memoizedProps&&n.memoizedProps.onChange){n.memoizedProps.onChange({target:{checked:true},preventDefault:function(){},stopPropagation:function(){}});break;}n=n.return;}});var btn=document.querySelectorAll('button');for(var i=0;i<btn.length;i++){if(btn[i].textContent.trim()==='Descargar etiquetas'&&!btn[i].disabled){btn[i].click();break;}}return'ok';})();
  " 2>/dev/null
  sleep 2
  # Modal Normal + Download
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var a=document.querySelectorAll('*');for(var i=0;i<a.length;i++){if(a[i].textContent.trim()==='Normal'&&a[i].children.length===0){a[i].click();break;}}var b=document.querySelectorAll('button');for(var i=0;i<b.length;i++){if(b[i].textContent.trim()==='Descargar etiquetas'&&b[i].closest('[role=dialog]')){b[i].click();break;}}return'ok';})();
  " 2>/dev/null
  sleep 2
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var b=document.querySelectorAll('button');for(var i=0;i<b.length;i++){if(b[i].textContent.trim()==='Confirmar'&&!b[i].disabled){b[i].click();break;}}return'ok';})();
  " 2>/dev/null
  sleep 3

  # 上传产品标签
  LABEL_FILE=$(ls -t "$ZINIAO_DL"/Etiquetas-de-producto-*.pdf 2>/dev/null | head -1)
  if [ -n "$LABEL_FILE" ]; then
    LABEL_NAME=$(basename "$LABEL_FILE")
    upload_file "$RECORD_ID" "产品标签" "$LABEL_NAME"
    push_feishu "✅ 步骤6完成: 标签已上传"
  fi

  update_step "$RECORD_ID" "步骤7：箱唛打印"

  # ── 步骤7：箱唛 ──
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var c=document.querySelectorAll('[role=button]');for(var i=0;i<c.length;i++){if(c[i].textContent.includes('bultos')){c[i].click();break;}}return'ok';})();
  " 2>/dev/null
  sleep 3
  # 填10箱 + No enviaré
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var c=document.getElementById('checkVolumesId');if(c)c.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,view:window}));var q=document.getElementById('labelsQuantity');if(q){q.click();q.focus();var ns=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;ns.call(q,'$BOX');q.dispatchEvent(new Event('input',{bubbles:true}));}document.querySelectorAll('[data-andes-checkbox-container]').forEach(function(cb,i){var k=Object.keys(cb).find(function(x){return x.startsWith('__reactFiber')});if(!k)return;var n=cb[k];for(var j=0;j<15&&n;j++){if(n.memoizedProps&&n.memoizedProps.onChange){n.memoizedProps.onChange({target:{checked:i>=1},preventDefault:function(){},stopPropagation:function(){}});break;}n=n.return;}});return'ok';})();
  " 2>/dev/null
  sleep 1
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var b=document.querySelectorAll('button');for(var i=0;i<b.length;i++){if(b[i].textContent.trim()==='Generar etiquetas'&&!b[i].disabled){b[i].click();return'generated';}}return'fail';})();
  " 2>/dev/null
  sleep 5
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var b=document.querySelectorAll('button');for(var i=0;i<b.length;i++){if(b[i].textContent.includes('Descarga todas')){b[i].click();break;}}return'ok';})();
  " 2>/dev/null
  sleep 2
  # Modal
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var a=document.querySelectorAll('*');for(var i=0;i<a.length;i++){if(a[i].textContent.trim()==='Normal'&&a[i].children.length===0){a[i].click();break;}}var b=document.querySelectorAll('button');for(var i=0;i<b.length;i++){if(b[i].textContent.trim()==='Descargar etiquetas'&&!!b[i].closest('[role=dialog]')){b[i].click();break;}}return'ok';})();
  " 2>/dev/null
  sleep 3

  # 上传箱唛
  BOX_FILE=$(ls -t "$ZINIAO_DL"/Envio-*-Etiquetas-de-bultos.pdf 2>/dev/null | head -1)
  if [ -n "$BOX_FILE" ]; then
    BOX_NAME=$(basename "$BOX_FILE")
    upload_file "$RECORD_ID" "箱唛" "$BOX_NAME"
  fi

  update_step "$RECORD_ID" "步骤8：取消预约"
  push_feishu "✅ 步骤7完成: 箱唛已上传"

  # ── 步骤8：取消预约 ──
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var l=document.querySelectorAll('a');var c=0;for(var i=0;i<l.length;i++){if(l[i].textContent.trim()==='Editar'){c++;if(c===2){l[i].click();break;}}}return'ok';})();
  " 2>/dev/null
  sleep 3
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){window.scrollTo(0,1500);var b=document.querySelectorAll('button');for(var i=0;i<b.length;i++){if(b[i].textContent.trim()==='Cancelar reserva'){b[i].click();return'ok';}}return'fail';})();
  " 2>/dev/null
  sleep 1
  ziniao-cli page exec --store-id "$STORE_ID" --script "
  (function(){var b=document.querySelectorAll('button');for(var i=0;i<b.length;i++){if(b[i].textContent.trim()==='Cancelar cita'){b[i].click();break;}}return'ok';})();
  " 2>/dev/null

  # ── 完成 ──
  update_field "$RECORD_ID" "状态" '"已完成"'
  update_step "$RECORD_ID" "全部完成"
  update_field "$RECORD_ID" "就绪" "false"   # 取消勾选，防止重复触发
  ziniao-cli store close --id "$STORE_ID" 2>/dev/null

  push_feishu "🎉 FULL 货件完成！\nSKU: $SKU $NAME\n货件: #$SHIPMENT\n数量: ${QTY}件 / ${BOX}箱\n文件已上传到飞书多维表格"
  echo "✅ $SKU 完成"
done