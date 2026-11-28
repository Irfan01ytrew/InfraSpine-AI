import { useRef, useState } from 'react';
import type { Kind, Spec, Step, Validation } from './types';
import { connectNodes, deleteNode, newNode, nodeKinds, arrange } from './specEdit';

export { arrange, newNode, nodeKinds };

export function FlowDiagram({spec,onChange,selected,onSelect,errors=[],steps=[]}: {
  spec:Spec;onChange?:(spec:Spec)=>void;selected?:string;onSelect?:(id:string)=>void;errors?:Validation[];steps?:Step[];
}) {
  const [zoom,setZoom]=useState(0.8); const [connecting,setConnecting]=useState<string|null>(null);
  const svg=useRef<SVGSVGElement>(null); const drag=useRef<{id:string;x:number;y:number;ox:number;oy:number}|null>(null);
  const width=Math.max(1100,...spec.nodes.map(n=>n.x+280)); const height=Math.max(480,...spec.nodes.map(n=>n.y+160));
  const nodeMap=new Map(spec.nodes.map(n=>[n.id,n])); const statuses=new Map(steps.map(s=>[s.node_id,s.status]));
  const connect=(target:string)=>{
    if(!onChange || !connecting || connecting===target) return;
    onChange(connectNodes(spec, connecting, target));
    setConnecting(null);
  };
  return <div className="wf-diagram-shell">
    {onChange&&<div className="wf-palette" role="toolbar" aria-label="Add a step">
      {nodeKinds.map(k=><button key={k.kind} type="button" draggable
        onDragStart={e=>{e.dataTransfer.setData('application/infraspine-node',k.kind);e.dataTransfer.effectAllowed='copy';}}
        onClick={()=>{const n=newNode(k.kind,100,100);onChange({...spec,nodes:[...spec.nodes,n]});onSelect?.(n.id);}}
        title={`Add ${k.label}`}>{k.symbol} {k.label}</button>)}
    </div>}
    <div className="wf-diagram-tools"><span>{connecting?'Select a destination node':onChange?'Drag nodes · connect using the right port':'Persisted execution graph'}</span>
      <div className="wf-inline"><button onClick={()=>setZoom(z=>Math.max(.3,z-.1))} aria-label="Zoom out">−</button><span>{Math.round(zoom*100)}%</span><button onClick={()=>setZoom(z=>Math.min(1.5,z+.1))} aria-label="Zoom in">+</button>
      <button onClick={()=>setZoom(Math.min(.9,900/width))}>Fit</button>{onChange&&<button onClick={()=>onChange(arrange(spec))}>Auto-layout</button>}
      {onChange&&selected&&spec.nodes.some(n=>n.id===selected)&&<button onClick={()=>onChange(deleteNode(spec,selected))}>Delete selected node</button>}
      {connecting&&<button onClick={()=>setConnecting(null)}>Cancel connector</button>}</div>
    </div>
    <div className="wf-canvas" onDragOver={e=>{if(onChange)e.preventDefault();}} onDrop={e=>{
      e.preventDefault(); const kind=e.dataTransfer.getData('application/infraspine-node') as Kind;
      if(!onChange||!nodeKinds.some(n=>n.kind===kind)||!svg.current)return;
      const r=svg.current.getBoundingClientRect(); const n=newNode(kind,Math.max(0,(e.clientX-r.left)/zoom),Math.max(0,(e.clientY-r.top)/zoom));
      onChange({...spec,nodes:[...spec.nodes,n]});onSelect?.(n.id);
    }}>
      <svg ref={svg} width={width*zoom} height={height*zoom} viewBox={`0 0 ${width} ${height}`} aria-label="Executable workflow diagram" role="group"
        onPointerMove={e=>{if(!drag.current||!onChange)return; const d=drag.current;onChange({...spec,nodes:spec.nodes.map(n=>n.id===d.id?{...n,x:Math.max(0,Math.min(12000,d.ox+(e.clientX-d.x)/zoom)),y:Math.max(0,Math.min(12000,d.oy+(e.clientY-d.y)/zoom))}:n)});}}
        onPointerUp={()=>{drag.current=null;}} onPointerCancel={()=>{drag.current=null;}}>
        <defs><marker id="wf-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8"/></marker><pattern id="wf-dots" width="22" height="22" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1"/></pattern></defs>
        <rect width={width} height={height} fill="url(#wf-dots)" className="wf-dot-grid"/>
        {spec.edges.map((e,i)=>{const a=nodeMap.get(e.source),b=nodeMap.get(e.target);if(!a||!b)return null;const x=a.x+220,y=a.y+42,tx=b.x,ty=b.y+42;const bend=Math.max(60,Math.abs(tx-x)/2);
          return <g key={`${e.source}-${e.target}-${i}`}><path className="wf-edge" d={`M${x},${y} C${x+bend},${y} ${tx-bend},${ty} ${tx},${ty}`} markerEnd="url(#wf-arrow)"/>
            {e.branch&&e.branch!=='always'&&<text className="wf-edge-label" x={(x+tx)/2} y={(y+ty)/2-9}>{e.branch}</text>}</g>;})}
        {spec.nodes.map(n=>{const error=errors.some(e=>e.node_id===n.id); const status=statuses.get(n.id);return <g key={n.id} transform={`translate(${n.x},${n.y})`} tabIndex={0} role="button" aria-label={`${n.label}, ${n.type}${status?', '+status:''}`} aria-pressed={selected===n.id}
          className={`wf-node wf-node-${n.type}${selected===n.id?' selected':''}${error?' invalid':''}${status?' status-'+status:''}`}
          onKeyDown={e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();connecting?connect(n.id):onSelect?.(n.id);}if(onChange&&['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(e.key)){e.preventDefault();onChange({...spec,nodes:spec.nodes.map(p=>p.id!==n.id?p:{...p,x:Math.max(0,p.x+(e.key==='ArrowRight'?10:e.key==='ArrowLeft'?-10:0)),y:Math.max(0,p.y+(e.key==='ArrowDown'?10:e.key==='ArrowUp'?-10:0))})});}}}
          onPointerDown={e=>{if(connecting){connect(n.id);return;}onSelect?.(n.id);if(onChange){drag.current={id:n.id,x:e.clientX,y:e.clientY,ox:n.x,oy:n.y};e.currentTarget.setPointerCapture(e.pointerId);}}}>
          <rect width="220" height="84" rx="12"/><rect className="wf-node-accent" width="4" height="52" y="16" rx="2"/>
          <text x="18" y="28" className="wf-node-kind">{nodeKinds.find(k=>k.kind===n.type)?.symbol} {n.type.toUpperCase()}</text>
          <text x="18" y="51" className="wf-node-title">{n.label.length>26?n.label.slice(0,24)+'…':n.label}</text>
          <text x="18" y="71" className="wf-node-meta">{status||`${n.risk||'low'} risk${n.config.assignee_id?' · assigned':''}`}</text>
          {onChange&&<circle cx="220" cy="42" r="7" className="wf-port" role="button" tabIndex={0} aria-label={`Connect from ${n.label}`}
            onPointerDown={e=>{e.stopPropagation();setConnecting(n.id);}} onKeyDown={e=>{if(e.key==='Enter'){e.stopPropagation();setConnecting(n.id);}}}/>}
        </g>;})}
      </svg>
    </div>
  </div>;
}
