(function(){let e=document.createElement(`link`).relList;if(e&&e.supports&&e.supports(`modulepreload`))return;for(let e of document.querySelectorAll(`link[rel="modulepreload"]`))n(e);new MutationObserver(e=>{for(let t of e)if(t.type===`childList`)for(let e of t.addedNodes)e.tagName===`LINK`&&e.rel===`modulepreload`&&n(e)}).observe(document,{childList:!0,subtree:!0});function t(e){let t={};return e.integrity&&(t.integrity=e.integrity),e.referrerPolicy&&(t.referrerPolicy=e.referrerPolicy),e.crossOrigin===`use-credentials`?t.credentials=`include`:e.crossOrigin===`anonymous`?t.credentials=`omit`:t.credentials=`same-origin`,t}function n(e){if(e.ep)return;e.ep=!0;let n=t(e);fetch(e.href,n)}})();function e(e){return e==null||e===``?`-`:String(e)}function t(e){return e?new Date(e*1e3).toLocaleString():`-`}function n(e){let t=Math.max(0,Number(e||0));return t<60?`${Math.floor(t)}s`:t<3600?`${Math.floor(t/60)}m`:t<86400?`${Math.floor(t/3600)}h ${Math.floor(t%3600/60)}m`:`${Math.floor(t/86400)}d ${Math.floor(t%86400/3600)}h`}function r(e){return e?e.split(`
`)[0]??``:``}var i=[[`total`,`Total`],[`queued`,`Queued`],[`running`,`Running`],[`backlog`,`Backlog`],[`completed`,`Completed`],[`failed`,`Failed`],[`contexts`,`Contexts`]];function a(e){e.innerHTML=`
    <header>
      <div class="bar">
        <div>
          <h1>Nyanpasu Dashboard</h1>
          <div class="subtle" id="generated">Loading...</div>
        </div>
        <div class="toolbar">
          <span class="subtle" id="health">Waiting for data</span>
          <button id="refresh" type="button" title="Refresh dashboard data">Refresh</button>
        </div>
      </div>
    </header>
    <main>
      <section>
        <div class="metrics" id="metrics"></div>
      </section>
      <section class="grid">
        <div class="plugins">
          <h2>Plugins</h2>
          <div class="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Plugin</th>
                  <th class="num">Total</th>
                  <th class="num">Queued</th>
                  <th class="num">Running</th>
                  <th class="num">Done</th>
                  <th class="num">Failed</th>
                </tr>
              </thead>
              <tbody id="plugins"></tbody>
            </table>
          </div>
        </div>
        <div>
          <h2>Backlog</h2>
          <div class="table-wrap" id="backlog-wrap">
            <table>
              <thead>
                <tr>
                  <th>Status</th>
                  <th>Task</th>
                  <th>Plugin</th>
                  <th>Context</th>
                  <th class="num">Age</th>
                </tr>
              </thead>
              <tbody id="backlog"></tbody>
            </table>
          </div>
        </div>
      </section>
      <section>
        <h2>Recent Tasks</h2>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Status</th>
                <th>Task</th>
                <th>Plugin</th>
                <th>Source</th>
                <th>Action</th>
                <th>Updated</th>
                <th>Error</th>
              </tr>
            </thead>
            <tbody id="recent"></tbody>
          </table>
        </div>
      </section>
    </main>
  `}function o(e){s(e),c(e.plugins),l(e.backlog),u(e.recent),d(`generated`).textContent=`Generated ${t(e.generated_at)}`}function s(e){let t=d(`metrics`);f(t);for(let[n,r]of i){let i=document.createElement(`div`);i.className=`metric ${n}`;let a=document.createElement(`div`);a.className=`label`,a.textContent=r;let o=document.createElement(`div`);o.className=`value`,o.textContent=Number(e.totals[n]||0).toLocaleString(),i.append(a,o),t.appendChild(i)}}function c(e){let t=d(`plugins`);if(f(t),!e.length){h(t,6,`No plugin tasks recorded.`);return}for(let n of e){let e=document.createElement(`tr`);e.append(p(n.plugin_id,`mono`),p(n.total,`num`),p(n.queued,`num`),p(n.running,`num`),p(n.completed,`num`),p(n.failed,`num`)),t.appendChild(e)}}function l(e){let t=d(`backlog`);if(f(t),!e.length){h(t,5,`No queued or running tasks.`);return}for(let r of e){let e=document.createElement(`tr`);e.append(m(r.status),p(r.title,`title`),p(r.plugin_id,`mono`),p(r.context_key,`mono`),p(n(r.age_seconds),`num`)),t.appendChild(e)}}function u(e){let n=d(`recent`);if(f(n),!e.length){h(n,7,`No tasks recorded.`);return}for(let i of e){let e=document.createElement(`tr`);e.append(m(i.status),p(i.title,`title`),p(i.plugin_id,`mono`),p(i.source,`mono`),p(i.action,`mono`),p(t(i.updated_at)),p(r(i.error),`error`)),n.appendChild(e)}}function d(e){let t=document.getElementById(e);if(t===null)throw Error(`missing dashboard element: ${e}`);return t}function f(e){for(;e.firstChild;)e.removeChild(e.firstChild)}function p(t,n){let r=document.createElement(`td`);return n&&(r.className=n),r.textContent=e(t),r}function m(e){let t=document.createElement(`td`),n=document.createElement(`span`);return n.className=`status ${e}`,n.textContent=e,t.appendChild(n),t}function h(e,t,n){let r=document.createElement(`tr`),i=document.createElement(`td`);i.className=`empty`,i.colSpan=t,i.textContent=n,r.appendChild(i),e.appendChild(r)}var g=document.getElementById(`app`);if(g===null)throw Error(`missing #app element`);a(g);var _=document.getElementById(`refresh`);_!==null&&_.addEventListener(`click`,()=>{v()}),v(),window.setInterval(()=>{v()},15e3);async function v(){let e=document.getElementById(`health`);y(e,`Refreshing`);try{let t=await fetch(`/api/dashboard`,{cache:`no-store`});if(!t.ok)throw Error(`HTTP ${t.status}`);o(await t.json()),y(e,`Live`)}catch(t){y(e,`Error: ${t instanceof Error?t.message:String(t)}`)}}function y(e,t){e!==null&&(e.textContent=t)}