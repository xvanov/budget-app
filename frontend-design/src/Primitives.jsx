// Primitives: Button, Badge, Card, Input, Tabs/Tab, StatusBadge
// Lightly styled ShadCN analogs.

function Button({ variant = "default", size = "md", children, className = "", ...props }) {
  const base = "dod-btn";
  const v = `dod-btn--${variant}`;
  const s = size !== "md" ? `dod-btn--${size}` : "";
  return <button {...props} className={`${base} ${v} ${s} ${className}`}>{children}</button>;
}

function Badge({ variant = "default", children, className = "", style }) {
  return <span className={`dod-badge dod-badge--${variant} ${className}`} style={style}>{children}</span>;
}

function Card({ children, className = "", ...rest }) {
  return <div className={`dod-card ${className}`} {...rest}>{children}</div>;
}
function CardHeader({ children, className = "" }) { return <div className={`dod-card-header ${className}`}>{children}</div>; }
function CardTitle({ children, className = "" }) { return <div className={`dod-card-title ${className}`}>{children}</div>; }
function CardDescription({ children, className = "" }) { return <div className={`dod-card-desc ${className}`}>{children}</div>; }
function CardContent({ children, className = "" }) { return <div className={`dod-card-content ${className}`}>{children}</div>; }

function Input(props) { return <input className="dod-input" {...props} />; }

function Tabs({ value, onChange, options, size = "md" }) {
  return (
    <div className={`dod-tabs dod-tabs--${size}`}>
      {options.map(o => (
        <button key={o.value} onClick={() => onChange?.(o.value)}
          className={`dod-tabs-item ${value === o.value ? "is-active" : ""}`}>
          {o.label}
          {o.count != null && <span className="dod-tabs-count">{o.count}</span>}
        </button>
      ))}
    </div>
  );
}

function Segmented({ value, onChange, options }) {
  return (
    <div className="dod-seg">
      {options.map(o => (
        <button key={o.value} onClick={() => onChange?.(o.value)}
          className={`dod-seg-item ${value === o.value ? "is-active" : ""}`}>{o.label}</button>
      ))}
    </div>
  );
}

function StatusBadge({ status }) {
  switch (status) {
    case "Done":
    case "Accepted":
      return <Badge variant="success"><Icon name="CheckCircle2" size={12}/> {status}</Badge>;
    case "In Progress":
    case "Calculating":
      return <Badge variant="secondary"><span className="dod-dot dod-dot--primary"/> {status}</Badge>;
    case "In Review":
      return <Badge variant="outline"><Icon name="Clock" size={12}/> {status}</Badge>;
    case "Expired":
      return <Badge variant="destructive">{status}</Badge>;
    case "Declined":
      return <Badge variant="muted">{status}</Badge>;
    default:
      if (status?.startsWith("Urgent")) {
        return <Badge variant="warning"><span className="dod-dot dod-dot--yellow dod-dot--pulse"/> {status}</Badge>;
      }
      return <Badge variant="outline">{status}</Badge>;
  }
}

Object.assign(window, { Button, Badge, Card, CardHeader, CardTitle, CardDescription, CardContent, Input, Tabs, Segmented, StatusBadge });
