import type { TimelineSource } from "../api";

/** 回答下的资料来源卡：只展示程序实际记录的读取结果，不解析回答里的措辞。 */
export default function SourceCard({ sources }: { sources: TimelineSource[] }) {
  return (
    <section className="source-card" data-component="SourceCard" aria-label="资料来源">
      <div className="source-title">资料来源</div>
      {sources.map((source) => {
        const [start, end] = source.ref.lines;
        return (
          <div className="source-item" key={source.source_id}>
            <div className="source-head">
              <span className="source-name">{source.title ?? source.ref.path}</span>
              <span className="source-meta">
                <span className="source-path">{source.ref.path}</span>
                {source.ref.heading !== null && <span>{source.ref.heading}</span>}
                <span>L{start}–{end}</span>
                <span className="source-commit">{source.ref.commit.slice(0, 7)}</span>
              </span>
            </div>
            {source.excerpt !== "" && <p className="source-excerpt">{source.excerpt}</p>}
          </div>
        );
      })}
    </section>
  );
}
