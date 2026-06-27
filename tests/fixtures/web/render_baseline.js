
  traceView(){
    return this.state.trace.map((s,i)=>{
      const isRun = s.tool==='run_select'||s.tool==='search_text';
      const color = s.status==='ok'?'#15803D':s.status==='rejected'?'#0B7A6E':s.status==='error'?'#DC2626':'#2F6FED';
      const argText = s.tool==='describe_table'?'("'+s.args.table+'")'
        : s.tool==='search_text'?'("'+s.args.term+'")'
        : s.tool==='run_select'?'( … )':'()';
      let receipt=null;
      if(isRun && s.status!=='running'){
        if(s.status==='rejected') receipt=[{l:'guard',m:'🛡',c:'#0B7A6E'},{l:'txn',m:'⊘',c:'#C6CCD4'},{l:'role',m:'⊘',c:'#C6CCD4'}];
        else if(s.status==='ok') receipt=[{l:'guard',m:'✓',c:'#3D52CC'},{l:'txn',m:'✓',c:'#0E72A8'},{l:'role',m:'✓',c:'#0B7A6E'}];
        else receipt=[{l:'guard',m:'✓',c:'#3D52CC'},{l:'txn',m:'✕',c:'#DC2626'},{l:'role',m:'·',c:'#C6CCD4'}];
      }
      return { idx:i+1, tool:s.tool, argText, color, running:s.status==='running',
        statusLabel: s.status==='running'?'running…':s.status,
        latency: s.latency!=null?(s.latency+'ms'):'', sql: s.args.sql||null,
        isRun, hasReceipt: !!receipt, receipt: receipt||[], reason: s.error||null };
    });
  }

  auditView(){
    return this.state.audit.map(a=>{
      const edge = a.status==='ok'?'#15803D':a.status==='rejected'?'#0B7A6E':'#DC2626';
      return { edge, json: JSON.stringify(a) };
    });
  }

  boundaryView(){
    const b=this.state.boundary;
    const band={l1:'#3D52CC',l2:'#0E72A8',l3:'#0B7A6E'};
    const col=(v,layer)=> v==='pass'?band[layer]:v==='reject'?'#0B7A6E':v==='ghost'?'#C6CCD4':v==='error'?'#DC2626':'#9AA0AA';
    const mark=(v)=> v==='pass'?'✓':v==='reject'?'🛡':v==='ghost'?'⊘':v==='error'?'✕':'·';
    const opa=(v)=> v==='ghost'?'.5':'1';
    let headline, footTitle, footColor, footBg, tokenColor, tokenAnim;
    if(b.mode==='reject'){
      headline='L1 ✓ stop · L2 ⊘ · L3 ⊘ → write unreachable';
      footTitle='🛡 Rejected at Layer 1 — independently provable at Layers 2 & 3';
      footColor='#0B7A6E'; footBg='#E7F8F2';
      tokenColor='#0E9384'; tokenAnim='qgSlam .7s cubic-bezier(.2,.8,.1,1) forwards, qgRing .9s .55s ease';
    } else if(b.mode==='pass'){
      headline='L1 ✓ · L2 ✓ · L3 ✓ → ran';
      footTitle='Ran · query executed read-only';
      footColor='#15803D'; footBg='#ECFBF1';
      tokenColor='#0E72A8'; tokenAnim='qgTravel 1.05s ease forwards';
    } else if(b.mode==='error'){
      headline='reached Postgres · statement_timeout fired';
      footTitle='Timed out at Layer 2 (statement_timeout 5s)';
      footColor='#DC2626'; footBg='#FBECEC';
      tokenColor='#DC2626'; tokenAnim='qgTravel 1s ease forwards';
    } else {
      headline='Three independent layers. Any one would mostly hold; together, a write is unreachable.';
      footTitle='Idle — send a query to watch it travel the boundary';
      footColor='#6A727E'; footBg='#EEF0F4';
      tokenColor='#2F6FED'; tokenAnim='none';
    }
    return { active:b.active, mode:b.mode, sql:b.sql, reason:b.reason, headline,
      l1c:col(b.l1,'l1'), l2c:col(b.l2,'l2'), l3c:col(b.l3,'l3'), l1m:mark(b.l1), l2m:mark(b.l2), l3m:mark(b.l3),
      l2o:opa(b.l2), l3o:opa(b.l3), tokenColor, tokenAnim,
      footTitle, footColor, footBg };
  }
