using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Management;
using System.Net.Sockets;
using System.Windows.Forms;

public class HexStrikeControl : Form
{
    private readonly string serverDir;
    private readonly string pythonExe;
    private readonly string serverScript;
    private Button btnStart;
    private Button btnStop;
    private Button btnHome;
    private Button btnHealth;
    private Label statusLabel;
    private Label portLabel;
    private TextBox portBox;
    private System.Windows.Forms.Timer pollTimer;

    private int CurrentPort
    {
        get
        {
            int p;
            if (int.TryParse(portBox.Text.Trim(), out p) && p >= 1 && p <= 65535) return p;
            return 8888;
        }
    }

    private static readonly Color BgDark = Color.FromArgb(18, 4, 6);
    private static readonly Color BgPanel = Color.FromArgb(32, 8, 10);
    private static readonly Color Red = Color.FromArgb(255, 42, 50);
    private static readonly Color RedDark = Color.FromArgb(183, 28, 28);
    private static readonly Color Green = Color.FromArgb(0, 230, 118);
    private static readonly Color TextMain = Color.FromArgb(255, 245, 230);

    public HexStrikeControl()
    {
        serverDir = AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar);
        pythonExe = Path.Combine(serverDir, "hexstrike-env", "Scripts", "python.exe");
        serverScript = Path.Combine(serverDir, "hexstrike_server.py");

        Text = "HexStrike AI // Server Control";
        FormBorderStyle = FormBorderStyle.FixedSingle;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(460, 250);
        BackColor = BgDark;
        ForeColor = TextMain;
        Font = new Font("Consolas", 10f, FontStyle.Bold);

        var title = new Label
        {
            Text = "HEXSTRIKE AI v6.0",
            ForeColor = Red,
            Font = new Font("Consolas", 16f, FontStyle.Bold),
            AutoSize = false,
            TextAlign = ContentAlignment.MiddleCenter,
            Dock = DockStyle.Top,
            Height = 44
        };
        Controls.Add(title);

        statusLabel = new Label
        {
            Text = "● CHECKING...",
            ForeColor = Color.Gray,
            AutoSize = false,
            TextAlign = ContentAlignment.MiddleCenter,
            Dock = DockStyle.Top,
            Height = 26
        };
        Controls.Add(statusLabel);

        portLabel = new Label
        {
            Text = "api: http://127.0.0.1:8888/health",
            ForeColor = Color.DimGray,
            Font = new Font("Consolas", 8f),
            AutoSize = false,
            TextAlign = ContentAlignment.MiddleCenter,
            Dock = DockStyle.Top,
            Height = 20
        };
        Controls.Add(portLabel);

        var portRow = new Panel { Dock = DockStyle.Top, Height = 30, BackColor = BgDark };
        Controls.Add(portRow);
        var portLbl = new Label
        {
            Text = "PORT:",
            ForeColor = Color.DimGray,
            Font = new Font("Consolas", 9f, FontStyle.Bold),
            AutoSize = true,
            Left = 165,
            Top = 6
        };
        portRow.Controls.Add(portLbl);
        portBox = new TextBox
        {
            Text = "8888",
            Left = 215,
            Top = 3,
            Width = 70,
            BackColor = Color.FromArgb(20, 6, 8),
            ForeColor = TextMain,
            BorderStyle = BorderStyle.FixedSingle,
            Font = new Font("Consolas", 10f, FontStyle.Bold),
            TextAlign = HorizontalAlignment.Center
        };
        portBox.TextChanged += (s, e) =>
        {
            portLabel.Text = "api: http://127.0.0.1:" + CurrentPort + "/health";
            RefreshStatus();
        };
        portRow.Controls.Add(portBox);
        portRow.SendToBack();

        var panel = new Panel { Dock = DockStyle.Bottom, Height = 110, BackColor = BgPanel, Padding = new Padding(14) };
        Controls.Add(panel);

        btnStart = MakeButton("▶  START SERVER", RedDark, 14, 14, 200, 36);
        btnStart.Click += (s, e) => StartServer();
        panel.Controls.Add(btnStart);

        btnStop = MakeButton("■  STOP SERVER", Color.FromArgb(90, 20, 22), 246, 14, 200, 36);
        btnStop.Click += (s, e) => StopServer();
        panel.Controls.Add(btnStop);

        btnHome = MakeButton("⌂  HEXSTRIKE MAINPAGE", Color.FromArgb(60, 14, 16), 14, 60, 200, 36);
        btnHome.Click += (s, e) => Process.Start("http://127.0.0.1:" + CurrentPort + "/");
        panel.Controls.Add(btnHome);

        btnHealth = MakeButton("✚  API HEALTH", Color.FromArgb(60, 14, 16), 246, 60, 200, 36);
        btnHealth.Click += (s, e) => Process.Start("http://127.0.0.1:" + CurrentPort + "/health");
        panel.Controls.Add(btnHealth);

        pollTimer = new System.Windows.Forms.Timer { Interval = 2000 };
        pollTimer.Tick += (s, e) => RefreshStatus();
        pollTimer.Start();
        RefreshStatus();
    }

    private Button MakeButton(string text, Color bg, int x, int y, int w, int h)
    {
        return new Button
        {
            Text = text,
            Left = x,
            Top = y,
            Width = w,
            Height = h,
            BackColor = bg,
            ForeColor = TextMain,
            FlatStyle = FlatStyle.Flat,
            Font = new Font("Consolas", 9.5f, FontStyle.Bold),
            Cursor = Cursors.Hand
        };
    }

    private bool IsServerUp()
    {
        try
        {
            using (var client = new TcpClient())
            {
                var ar = client.BeginConnect("127.0.0.1", CurrentPort, null, null);
                return ar.AsyncWaitHandle.WaitOne(600) && client.Connected;
            }
        }
        catch { return false; }
    }

    private void RefreshStatus()
    {
        bool up = IsServerUp();
        statusLabel.Text = up ? "● SERVER RUNNING" : "● SERVER STOPPED";
        statusLabel.ForeColor = up ? Green : Red;
        btnStart.Enabled = !up;
        btnStop.Enabled = up;
    }

    private void StartServer()
    {
        if (!File.Exists(pythonExe))
        {
            MessageBox.Show("venv python not found:\n" + pythonExe, "HexStrike", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        if (!File.Exists(serverScript))
        {
            MessageBox.Show("hexstrike_server.py not found:\n" + serverScript, "HexStrike", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return;
        }
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = pythonExe,
                Arguments = "hexstrike_server.py --port " + CurrentPort,
                WorkingDirectory = serverDir,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden
            };
            psi.EnvironmentVariables["PYTHONUTF8"] = "1";
            psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            Process.Start(psi);
        }
        catch (Exception ex)
        {
            MessageBox.Show("Failed to start server:\n" + ex.Message, "HexStrike", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        RefreshStatus();
    }

    private void StopServer()
    {
        try
        {
            using (var searcher = new ManagementObjectSearcher(
                "SELECT ProcessId FROM Win32_Process WHERE CommandLine LIKE '%hexstrike_server.py%'"))
            {
                foreach (ManagementObject proc in searcher.Get())
                {
                    try { Process.GetProcessById(Convert.ToInt32(proc["ProcessId"])).Kill(); }
                    catch { }
                }
            }
        }
        catch (Exception ex)
        {
            MessageBox.Show("Failed to stop server:\n" + ex.Message, "HexStrike", MessageBoxButtons.OK, MessageBoxIcon.Error);
        }
        RefreshStatus();
    }

    [STAThread]
    public static void Main()
    {
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new HexStrikeControl());
    }
}
