import matplotlib.pyplot as plt
import pandas as pd
import os

strategy_colors = {
    "special": "#FF9999",  # Light red
    "special_punc": "#66B2FF",  # Light blue
    "special_punc_heavy_hitter": "#99FF99",  # Light green
    "special_punch_window_heavy_hitter": "#FFCC99",  # Light orange
    "full": "#CC99FF"  # Light purple
}

def create_layer_donut(filename, output_dir, layer):
    # Load CSV
    df = pd.read_csv(filename)
    
    # Filter for selected layer
    df_layer = df[df['layerID'] == layer]
    
    # Count how many heads chose each strategy
    strategy_counts = df_layer['strategy'].value_counts()
    
    # Labels and sizes for the donut
    labels = strategy_counts.index.tolist()
    sizes = strategy_counts.values.tolist()
    colors = [strategy_colors.get(strategy, "#DDDDDD") for strategy in labels]

    # Plot
    fig, ax = plt.subplots()
    wedges, texts = ax.pie(sizes, labels=labels, startangle=90, wedgeprops=dict(width=0.4), colors=colors)
    ax.set_title(f"Layer {layer} - Strategy Distribution Across Heads")

    # Draw white circle in center to make it a donut
    center_circle = plt.Circle((0, 0), 0.70, fc='white')
    fig.gca().add_artist(center_circle)

    plt.axis('equal')
    
    os.makedirs(output_dir, exist_ok=True)
    output_file = os.path.join(output_dir, f"strategy_layer_{layer}.png")
    plt.savefig(output_file, dpi=300, bbox_inches='tight')
    plt.close(fig)
    print(f"written f'strategy_layer_{layer}.png' to {output_dir}")
    
    
def visualize_head_strategies(filename, output_dir, layers=[]):
    print('hello')
    for layer in layers:
        create_layer_donut(filename, output_dir, layer)

    
if __name__ == "__main__":
    layers = [0, 6, 11, 16, 20, 24, 31]
    filename = "/home/artij/mlsys/coldcompress/profiling_results/per_head_profiling.csv"
    output_dir = "/home/artij/mlsys/coldcompress/profiling_results/visuals"
    visualize_head_strategies(filename, output_dir, layers)