// Exercise production time/observation logic with independent market scenarios.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../templates/intraday_board.html'),'utf8');
const script=html.split('<script>')[1].split('</script>')[0];
new vm.Script(script); // Check all production JS, not just the pure functions below.
const ctx=vm.createContext({Intl,Date});
vm.runInContext(script.slice(script.indexOf('function chinaTime'),script.indexOf('function updateHealth')),ctx);
const now=Date.parse('2026-10-27T10:00:00+08:00');
const state={taken_at:'2026-10-27 09:59:50',quote_time:'20261027095945',quote_date:'2026-10-27'};
let checks=0;
function check(actual,expected){assert.equal(actual,expected);checks++;}
check(ctx.chinaTime('20261027095945'),Date.parse('2026-10-27T01:59:45Z'));
check(ctx.healthOf(state,true,now).level,'ok');
check(ctx.healthOf(state,false,now).label,'连接中断');
check(ctx.healthOf({...state,quote_time:null},true,now).label,'源时间未知');
check(ctx.healthOf({...state,quote_time:'20261026150000'},true,now).label,'行情滞后');
check(ctx.healthOf({...state,taken_at:'2026-10-27 09:55:00'},true,now).label,'行情滞后');
check(ctx.healthOf({...state,quote_time:'20261027110000'},true,now).label,'时钟异常');
check(ctx.healthOf(state,true,Date.parse('2026-10-27T12:00:00+08:00')).label,'非连续交易时段');
check(ctx.healthOf({booting:true},true,now).label,'等待数据');
// Fixed current time for watch comparisons, independent of developer machine time.
vm.runInContext(`Date.now=()=>${now}`,ctx);
const w={data_date:'2026-10-27',ts:'2026-10-27 09:58:00',entry_price:10};
const q={price:11,quote_time:state.quote_time};
check(ctx.canCompareWatch(w,q,state,true),true);
check(ctx.canCompareWatch({...w,data_date:'20261027'},q,state,true),true);
check(ctx.canCompareWatch({...w,data_date:'2026-10-26'},q,state,true),false);
check(ctx.canCompareWatch({...w,entry_price:0},q,state,true),false);
check(ctx.canCompareWatch(w,{price:11},state,true),false);
check(ctx.canCompareWatch(w,{...q,quote_time:'20261027093000'},state,true),false);
check(ctx.canCompareWatch(w,q,state,false),false);
check(ctx.canCompareWatch({...w,ts:'2026-10-28 00:01:00'},q,state,true),false);
console.log(checks+' board time and observation checks passed');
