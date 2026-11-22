"""Center dialogs after Windows has mapped their requested size."""
def center_popup(window, parent):
    parent=parent.winfo_toplevel()
    window.transient(parent)
    placed=False
    def place():
        nonlocal placed
        if placed or not window.winfo_exists() or not parent.winfo_exists():return
        if not window.winfo_ismapped():return
        window.update_idletasks()
        width=window.winfo_width();height=window.winfo_height()
        if width<=1 or height<=1:
            window.after(30,place);return
        x=parent.winfo_rootx()+(parent.winfo_width()-width)//2
        y=parent.winfo_rooty()+(parent.winfo_height()-height)//2
        x-=window.winfo_rootx()-window.winfo_x()
        y-=window.winfo_rooty()-window.winfo_y()
        window.geometry(f'{width}x{height}+{x}+{y}')
        placed=True
    def mapped(event):
        if event.widget is window:window.after(50,place)
    window.bind('<Map>',mapped,add='+')
    if window.winfo_ismapped():window.after(50,place)
