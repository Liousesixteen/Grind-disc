const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const code=fs.readFileSync(require('node:path').join(__dirname,'../templates/product-v2.js'),'utf8');new vm.Script(code);
const scope=vm.createContext({Date,Math,JSON,Number,String,esc:String});
const weekly=code.slice(code.indexOf('function weeklyBars('),code.indexOf('function drawCandleChart('));vm.runInContext(weekly,scope);
let r=scope.weeklyBars([{date:'2026-10-19',open:10,high:12,low:9,close:11,volume:100},{date:'2026-10-20',open:11,high:13,low:10,close:12,volume:200},{date:'2026-10-26',open:12,high:14,low:11,close:13,volume:300}]);
assert.equal(r.length,2);assert.equal(r[0].open,10);assert.equal(r[0].close,12);assert.equal(r[0].high,13);assert.equal(r[0].low,9);assert.equal(r[0].volume,300);
vm.runInContext(code.slice(code.indexOf('function observed('),code.indexOf('function ruleTable(')),scope);assert.equal(scope.observed(0),'0');assert.equal(scope.observed(false),'否');assert.equal(scope.observed(null),'缺有效输入');
const html=fs.readFileSync(require('node:path').join(__dirname,'../templates/intraday_board_v2.html'),'utf8');new vm.Script(html.split('<script>')[1].split('</script>')[0]);assert.match(html,/service-banner/);assert.match(html,/product-v2.js/);console.log('12 research UI/chart assertions passed');
