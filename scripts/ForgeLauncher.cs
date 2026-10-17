// Small Windows GUI launcher compiled locally by Setup FORGE.bat.
// No downloaded launcher executable, global service, or elevated permissions.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

internal static class ForgeLauncher {
    // Windows argv quoting: backslashes immediately before a quote/end must double.
    private static string Quote(string value) {
        StringBuilder result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value) {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') {
                result.Append('\\', slashes * 2 + 1); result.Append('"');
            } else {
                result.Append('\\', slashes); result.Append(c);
            }
            slashes = 0;
        }
        result.Append('\\', slashes * 2); result.Append('"');
        return result.ToString();
    }
    [STAThread]
    private static void Main(string[] args) {
        try {
            string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            string python = Path.Combine(root, @".venv\Scripts\pythonw.exe");
            if (!File.Exists(python)) {
                MessageBox.Show("Run Setup FORGE.bat in this folder first.", "FORGE setup required",
                    MessageBoxButtons.OK, MessageBoxIcon.Information); return;
            }
            StringBuilder arguments = new StringBuilder(Quote(Path.Combine(root, "launcher.py")));
            foreach (string arg in args) { arguments.Append(' '); arguments.Append(Quote(arg)); }
            ProcessStartInfo start = new ProcessStartInfo(python, arguments.ToString());
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            Process.Start(start);
        } catch (Exception error) {
            MessageBox.Show(error.Message, "FORGE could not open", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
    }
}
