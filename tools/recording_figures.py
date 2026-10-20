"""English, 600 dpi figures for standalone spike recording analysis."""
from pathlib import Path
import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import LinearSegmentedColormap

COLORS = ['#A4C8E1','#F8B400','#F26B59','#FFC0CB','#303C43','#3B4252','#A3BE8C','#BF616A','#D8DEE9']


def _style():
    plt.rcParams.update({'font.family':'DejaVu Sans','font.size':7,
        'axes.labelcolor':'#303C43','xtick.color':'#303C43','ytick.color':'#303C43',
        'text.color':'#303C43','axes.edgecolor':'#303C43','figure.facecolor':'white',
        'axes.facecolor':'white','svg.fonttype':'none','pdf.fonttype':42})


def _save(fig, stem):
    stem = Path(stem); stem.parent.mkdir(parents=True,exist_ok=True)
    fig.tight_layout()
    try:
        for ext in ('png','pdf','svg'):
            fig.savefig(str(stem)+'.'+ext,dpi=600,facecolor='white',bbox_inches='tight')
    finally:
        plt.close(fig)


def recording_figures(session,population,electrodes,bursts,output,name,limit):
    _style(); output = Path(output)
    fig,axes = plt.subplots(2,1,figsize=(6.5,3.7),sharex=True)
    indices = np.linspace(0,max(0,len(session.times)-1),min(len(session.times),int(limit)),dtype=int)
    axes[0].scatter(session.times[indices],session.channels[indices],s=.04,color=COLORS[4],
                    linewidths=0,rasterized=True)
    axes[0].set(ylabel='Recording channel',title=f'{name}: raster ({len(indices)} / {len(session.times)} spikes displayed)')
    axes[1].plot([(r['start_sec']+r['end_sec'])/2 for r in population],
                 [r['population_rate_hz'] for r in population],color=COLORS[0],lw=.7)
    for row in bursts:
        axes[1].axvspan(row['start_sec'],row['end_sec'],color=COLORS[2],alpha=.15)
    axes[1].set(xlabel='Time (s)',ylabel='Population rate (spikes/s)',xlim=(0,session.duration_sec))
    _save(fig,output/'activity')
    fig,ax = plt.subplots(figsize=(4,2.8))
    cmap = LinearSegmentedColormap.from_list('activity',['#7f3f98','#e46240','#ffde17'])
    points = ax.scatter([r['x_um'] for r in electrodes],[r['y_um'] for r in electrodes],
                        c=[r['firing_rate_hz'] for r in electrodes],s=5,cmap=cmap,rasterized=True)
    fig.colorbar(points,ax=ax,label='Firing rate (Hz)')
    ax.set(xlabel='X (um)',ylabel='Y (um)',title=f'{name}: all mapped electrodes',aspect='equal')
    ax.invert_yaxis(); _save(fig,output/'firing_rate_map')


def response_figures(trials,summaries,psths,output,name):
    _style(); output = Path(output)
    groups = {r['group_id']:[p for p in psths if p['group_id']==r['group_id']] for r in summaries}
    for page,start in enumerate(range(0,len(summaries),9),1):
        subset = summaries[start:start+9]
        columns = min(3,len(subset)); rows = (len(subset)+columns-1)//columns
        fig,axes = plt.subplots(rows,columns,figsize=(2.3*columns,2.15*rows),squeeze=False)
        for ax,row in zip(axes.flat,subset):
            points = groups[row['group_id']]
            ax.plot([(r['start_ms']+r['end_ms'])/2 for r in points],
                    [r['population_rate_hz'] if r['population_rate_hz'] is not None else np.nan for r in points],
                    color=COLORS[0],lw=.7)
            ax.axvline(0,color=COLORS[2],lw=.5)
            ax.set(title=f"{row['group_id']}: {row['kind']} dt={row['dt_ms']:g} ms",xlabel='From last pulse (ms)',ylabel='Spikes/s')
            if row.get('review_required'): ax.set_facecolor('#FFF3F5')
        for ax in list(axes.flat)[len(subset):]: ax.axis('off')
        fig.suptitle(f'{name}: PSTH; artifact bins omitted; tint = review',fontsize=7)
        _save(fig,output/f'psth_page_{page:02d}')
    good = [r for r in trials if r['net_count'] is not None]
    if good:
        fig,ax = plt.subplots(figsize=(6,2.5))
        for kind,color in zip(('single','synchronous','temporal'),COLORS):
            rows = [r for r in good if r['kind']==kind]
            if rows:
                ax.scatter([r['first_onset_sec']/60 for r in rows],[r['net_count'] for r in rows],
                           s=2,color=color,alpha=.5,label=kind,rasterized=True)
        ax.axhline(0,color=COLORS[4],lw=.4)
        ax.set(xlabel='Recording time (min)',ylabel='Post-last minus pre-first spike count',title=f'{name}: response drift')
        ax.legend(fontsize=6); _save(fig,output/'response_drift')
    temporal = {}
    for row in summaries:
        if row['kind']=='temporal' and len(row['electrodes'])==2:
            temporal.setdefault(tuple(row['electrodes']),[]).append(row)
    if temporal:
        fig,ax = plt.subplots(figsize=(5,3))
        for i,(pair,rows) in enumerate(temporal.items()):
            rows = sorted(rows,key=lambda r:r['dt_ms'])
            ax.plot([r['dt_ms'] for r in rows],
                    [r['success_fraction'] if r['success_fraction'] is not None else np.nan for r in rows],
                    marker=('o','s','^')[i//len(COLORS)%3],ms=2,lw=.7,color=COLORS[i%len(COLORS)],label=f'{pair[0]} to {pair[1]}')
        ax.set(xscale='log',xlabel='Actual-validated delay (ms)',ylabel='Response probability',ylim=(-.03,1.03),
               title='Descriptive delay curves; stages can drift over time')
        ax.legend(fontsize=5,ncol=2); _save(fig,output/'temporal_response')


def quality_figure(rows,stem):
    _style(); fig,ax = plt.subplots(figsize=(8,max(1.3,len(rows)*.35+.6))); ax.axis('off')
    columns = ['Recording','Status','Artifact 0-2 ms','Response 3-200 ms','Above quiet baseline']
    def number(v): return 'N/A' if v is None else f'{v:.1f}'
    cells = [[r['name'],r['status'],number(r.get('artifact_0_2ms_mean')),
              number(r.get('response_3_200ms_mean')),
              'N/A' if r.get('above_quiet_baseline') is None else 'Yes' if r['above_quiet_baseline'] else 'No'] for r in rows]
    table = ax.table(cellText=cells,colLabels=columns,cellLoc='center',loc='center')
    table.auto_set_font_size(False); table.set_fontsize(6)
    for (i,j),cell in table.get_celld().items():
        cell.set_edgecolor('#D8DEE9'); cell.set_linewidth(.3)
        cell.set_facecolor(COLORS[0] if i==0 else 'white' if rows[i-1]['status']=='ok' else COLORS[3])
    _save(fig,stem)


def comparison_figure(rows,stem):
    _style(); fig,ax = plt.subplots(figsize=(3.6,3.2))
    a,b = [r['pre_rate_hz'] for r in rows],[r['post_rate_hz'] for r in rows]
    ax.scatter(a,b,s=5,color=COLORS[0]); end = max(a+b+[.01])
    ax.plot([0,end],[0,end],color=COLORS[4],lw=.5)
    ax.set(xlabel='Pre firing rate (Hz)',ylabel='Post firing rate (Hz)',
           title='Pre/post firing rates (descriptive)')
    _save(fig,stem)
