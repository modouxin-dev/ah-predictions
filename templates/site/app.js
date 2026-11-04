/* 先知 · 亚盘看板：VIP 门禁（前端）、表格排序/筛选、展开行、图表。无外部依赖。 */
(function(){
"use strict";
var KEY="xianzhi_vip_hash";
var HASH=(document.querySelector('meta[name="vip-hash"]')||{}).content||"";

/* ---- SHA-256（crypto.subtle 不可用时的纯 JS 实现） ---- */
function sha256js(ascii){
  function rr(v,a){return (v>>>a)|(v<<(32-a));}
  var mp=Math.pow,mw=mp(2,32),res="",words=[],bl=ascii.length*8,h=[],k=[],pc=0,cand={};
  for(var c=2;pc<64;c++){if(!cand[c]){for(var i=0;i<313;i+=c)cand[i]=c;h[pc]=(mp(c,.5)*mw)|0;k[pc++]=(mp(c,1/3)*mw)|0;}}
  h=h.slice(0,8);ascii+="\x80";while(ascii.length%64-56)ascii+="\x00";
  for(i=0;i<ascii.length;i++){var j=ascii.charCodeAt(i);words[i>>2]|=j<<((3-i)%4)*8;}
  words[words.length]=((bl/mw)|0);words[words.length]=bl;
  for(j=0;j<words.length;){var w=words.slice(j,j+=16),oh=h;h=h.slice(0,8);
    for(i=0;i<64;i++){var w15=w[i-15],w2=w[i-2],a=h[0],e=h[4];
      var t1=h[7]+(rr(e,6)^rr(e,11)^rr(e,25))+((e&h[5])^((~e)&h[6]))+k[i]+(w[i]=(i<16)?w[i]:(w[i-16]+(rr(w15,7)^rr(w15,18)^(w15>>>3))+w[i-7]+(rr(w2,17)^rr(w2,19)^(w2>>>10)))|0);
      var t2=(rr(a,2)^rr(a,13)^rr(a,22))+((a&h[1])^(a&h[2])^(h[1]&h[2]));
      h=[(t1+t2)|0].concat(h);h[4]=(h[4]+t1)|0;}
    for(i=0;i<8;i++)h[i]=(h[i]+oh[i])|0;}
  for(i=0;i<8;i++)for(j=3;j+1;j--){var b=(h[i]>>(j*8))&255;res+=((b<16)?0:"")+b.toString(16);}
  return res;
}
function sha256(s){
  var bytes=unescape(encodeURIComponent(s));
  if(window.crypto&&crypto.subtle&&window.TextEncoder){
    return crypto.subtle.digest("SHA-256",new TextEncoder().encode(s)).then(function(b){
      return Array.prototype.map.call(new Uint8Array(b),function(x){return ("0"+x.toString(16)).slice(-2);}).join("");
    }).catch(function(){return sha256js(bytes);});
  }
  return Promise.resolve(sha256js(bytes));
}

/* ---- VIP ---- */
function setVip(on){
  document.body.classList.toggle("vip-on",on);
  var b=document.getElementById("vipbtn");
  if(b){b.textContent=on?"VIP · 退出":"🔒 VIP";b.classList.toggle("on",on);b.setAttribute("aria-pressed",on?"true":"false");}
  if(on)drawCharts();
}
function isVip(){try{return HASH&&localStorage.getItem(KEY)===HASH;}catch(e){return false;}}
function openModal(){var m=document.getElementById("vipmodal");m.classList.add("open");var i=m.querySelector("input");i.value="";m.querySelector(".err").textContent="";setTimeout(function(){i.focus();},50);}
function closeModal(){document.getElementById("vipmodal").classList.remove("open");}
function tryLogin(){
  var m=document.getElementById("vipmodal"),v=m.querySelector("input").value.trim();
  if(!v)return;
  sha256(v).then(function(h){
    if(h===HASH){try{localStorage.setItem(KEY,h);}catch(e){}closeModal();setVip(true);}
    else{m.querySelector(".err").textContent="密码不正确";}
  });
}
function initVip(){
  var b=document.getElementById("vipbtn");
  if(b)b.addEventListener("click",function(){
    if(document.body.classList.contains("vip-on")){if(confirm("退出 VIP？")){try{localStorage.removeItem(KEY);}catch(e){}setVip(false);}}
    else openModal();
  });
  Array.prototype.forEach.call(document.querySelectorAll(".vip-lock"),function(el){el.addEventListener("click",openModal);});
  var m=document.getElementById("vipmodal");
  if(m){
    m.querySelector(".ok").addEventListener("click",tryLogin);
    m.querySelector(".cancel").addEventListener("click",closeModal);
    m.querySelector("input").addEventListener("keydown",function(e){if(e.key==="Enter")tryLogin();if(e.key==="Escape")closeModal();});
    m.addEventListener("click",function(e){if(e.target===m)closeModal();});
  }
  setVip(!!isVip());
}

/* ---- 表格排序 ---- */
function cellVal(td){var v=td.getAttribute("data-v");if(v===null)v=td.textContent.trim();var n=parseFloat(v);return (v!==""&&!isNaN(n)&&/^[-+]?\d/.test(v))?n:v;}
function initSort(){
  Array.prototype.forEach.call(document.querySelectorAll("table.sortable"),function(tbl){
    var ths=tbl.querySelectorAll("thead th");
    Array.prototype.forEach.call(ths,function(th,idx){
      th.classList.add("sortable");th.setAttribute("tabindex","0");
      function go(){
        var asc=!th.classList.contains("asc");
        Array.prototype.forEach.call(ths,function(t){t.classList.remove("asc","desc");});
        th.classList.add(asc?"asc":"desc");
        var tb=tbl.tBodies[0],rows=Array.prototype.filter.call(tb.rows,function(r){return !r.classList.contains("detail")&&!r.classList.contains("emptyrow");});
        rows.sort(function(a,b){var x=cellVal(a.cells[idx]),y=cellVal(b.cells[idx]);
          if(typeof x==="number"&&typeof y==="number")return asc?x-y:y-x;
          return asc?String(x).localeCompare(String(y),"zh"):String(y).localeCompare(String(x),"zh");});
        rows.forEach(function(r){tb.appendChild(r);var d=document.getElementById("d-"+r.getAttribute("data-id"));if(d)tb.appendChild(d);});
      }
      th.addEventListener("click",go);th.addEventListener("keydown",function(e){if(e.key==="Enter")go();});
    });
  });
}

/* ---- 推荐记录：展开 + 筛选 ---- */
function initRecords(){
  var tbl=document.getElementById("records");if(!tbl)return;
  Array.prototype.forEach.call(tbl.querySelectorAll("tr.rec"),function(r){
    r.addEventListener("click",function(){var d=document.getElementById("d-"+r.getAttribute("data-id"));if(d){d.hidden=!d.hidden;r.setAttribute("aria-expanded",String(!d.hidden));}});
  });
  var f=document.getElementById("filters");if(!f)return;
  function apply(){
    var lg=f.league.value,from=f.from.value,to=f.to.value,res=f.result.value,n=0;
    Array.prototype.forEach.call(tbl.querySelectorAll("tr.rec"),function(r){
      var ok=(!lg||r.dataset.league===lg)&&(!from||r.dataset.date>=from)&&(!to||r.dataset.date<=to)&&
             (!res||r.dataset.status===res||r.dataset.outcome===res);
      r.hidden=!ok;var d=document.getElementById("d-"+r.dataset.id);if(d&&!ok)d.hidden=true;if(ok)n++;
    });
    var c=document.getElementById("rec-count");if(c)c.textContent=n;
  }
  ["league","from","to","result"].forEach(function(k){f[k].addEventListener("change",apply);f[k].addEventListener("input",apply);});
  f.reset.addEventListener("click",function(e){e.preventDefault();f.league.value="";f.from.value="";f.to.value="";f.result.value="";apply();});
  apply();
}

/* ---- 图表（SVG，随容器缩放，支持触摸） ---- */
function lineChart(el,data){
  var series=data.series,labels=data.labels,fmt=data.fmt||function(v){return v;};
  var W=el.clientWidth||600,H=el.clientHeight||240,P={l:44,r:12,t:14,b:28};
  var all=[];series.forEach(function(s){s.values.forEach(function(v){if(v!==null&&v!==undefined)all.push(v);});});
  if(!all.length){el.innerHTML='<div class="empty">暂无数据</div>';return;}
  var lo=Math.min.apply(null,all.concat(data.min!==undefined?[data.min]:[])),hi=Math.max.apply(null,all.concat(data.max!==undefined?[data.max]:[]));
  if(hi===lo){hi+=1;lo-=1;}
  var n=labels.length,x=function(i){return P.l+(n<=1?(W-P.l-P.r)/2:i*(W-P.l-P.r)/(n-1));},y=function(v){return P.t+(hi-v)*(H-P.t-P.b)/(hi-lo);};
  var s='<svg viewBox="0 0 '+W+' '+H+'" role="img">';
  for(var g=0;g<=4;g++){var gv=lo+(hi-lo)*g/4,gy=y(gv);s+='<line x1="'+P.l+'" x2="'+(W-P.r)+'" y1="'+gy+'" y2="'+gy+'" stroke="#e3e7ee"/><text x="'+(P.l-6)+'" y="'+(gy+4)+'" font-size="11" text-anchor="end" fill="#6b7385">'+fmt(gv)+'</text>';}
  if(lo<0&&hi>0)s+='<line x1="'+P.l+'" x2="'+(W-P.r)+'" y1="'+y(0)+'" y2="'+y(0)+'" stroke="#9aa1ad" stroke-dasharray="4 3"/>';
  var step=Math.max(1,Math.ceil(n/6));
  labels.forEach(function(l,i){if(i%step===0||i===n-1)s+='<text x="'+x(i)+'" y="'+(H-8)+'" font-size="11" text-anchor="middle" fill="#6b7385">'+l.slice(5)+'</text>';});
  series.forEach(function(se){var d="",pen=false;
    se.values.forEach(function(v,i){if(v===null||v===undefined){pen=false;return;}d+=(pen?"L":"M")+x(i)+" "+y(v);pen=true;});
    s+='<path d="'+d+'" fill="none" stroke="'+se.color+'" stroke-width="2.5"/>';
    se.values.forEach(function(v,i){if(v!==null&&v!==undefined)s+='<circle cx="'+x(i)+'" cy="'+y(v)+'" r="3" fill="'+se.color+'"/>';});
  });
  s+='<line class="cur" x1="0" x2="0" y1="'+P.t+'" y2="'+(H-P.b)+'" stroke="#1d2330" stroke-opacity=".3" style="display:none"/></svg><div class="tip"></div>';
  el.innerHTML=s;
  var tip=el.querySelector(".tip"),cur=el.querySelector(".cur");
  function show(ev){
    var r=el.getBoundingClientRect(),t=ev.touches?ev.touches[0]:ev,px=(t.clientX-r.left)*W/r.width;
    var i=n<=1?0:Math.round((px-P.l)*(n-1)/(W-P.l-P.r));i=Math.max(0,Math.min(n-1,i));
    var html="<b>"+labels[i]+"</b>"+(data.notes?"<br>"+data.notes[i]:"");
    series.forEach(function(se){var v=se.values[i];html+='<br><span style="color:'+se.color+'">●</span> '+se.name+"："+(v===null||v===undefined?"暂无":fmt(v));});
    tip.innerHTML=html;tip.style.display="block";
    var lx=x(i)*r.width/W;tip.style.left=Math.max(70,Math.min(r.width-70,lx))+"px";tip.style.top=(Math.min.apply(null,series.map(function(se){var v=se.values[i];return v===null||v===undefined?H:y(v);}))*r.height/H)+"px";
    cur.setAttribute("x1",x(i));cur.setAttribute("x2",x(i));cur.style.display="";
  }
  function hide(){tip.style.display="none";cur.style.display="none";}
  el.onmousemove=show;el.onmouseleave=hide;
  el.ontouchstart=show;el.ontouchmove=show;el.ontouchend=function(){setTimeout(hide,1500);};
}
var chartData=null;
function drawCharts(){
  var src=document.getElementById("chart-data");if(!src)return;
  if(!chartData)chartData=JSON.parse(src.textContent);
  var pct=function(v){return Math.round(v*100)+"%";},u=function(v){return (v>0?"+":"")+(Math.round(v*100)/100);};
  var c1=document.getElementById("chart-units"),c2=document.getElementById("chart-hit");
  if(!chartData.labels.length)return;
  if(c1&&c1.offsetParent!==null)lineChart(c1,{labels:chartData.labels,notes:chartData.notes,fmt:u,series:[{name:"累计盈亏(单位)",color:"#0b6e4f",values:chartData.cum}]});
  if(c2&&c2.offsetParent!==null)lineChart(c2,{labels:chartData.labels,fmt:pct,min:0,max:1,series:[{name:"近7天命中率",color:"#2459c9",values:chartData.hit7},{name:"近30天命中率",color:"#b7791f",values:chartData.hit30}]});
}
var rt;window.addEventListener("resize",function(){clearTimeout(rt);rt=setTimeout(drawCharts,150);});

document.addEventListener("DOMContentLoaded",function(){initVip();initSort();initRecords();drawCharts();});
})();
