# wallpaper_auto_downloader
Wallpaper Engine – Wallpaper Scheduled Automatic Switching Application<br/>
Automatically download Workshop items via steamcmd and use the WE application to switch wallpapers on a schedule<br/>
<br/>
The config requires the following parameters:<br/>
api_key (Steam Web API key, obtained from https://steamcommunity.com/dev/apikey)<br/>
interval (Scheduling interval, e.g., 3h / 90m / 1h30m; leave empty to execute only once)<br/>
filters (Set parameters according to your needs; to avoid inappropriate content, it is recommended to only include `Scene` and `Video` in `types`)<br/>
we_exe (Path to the Wallpaper Engine executable; right-click in Steam → Manage → Browse Local Files to find)<br/>
workshop_root (Path where WE will store wallpapers; it is recommended not to use the official Workshop path to prevent deletion of subscribed wallpapers)<br/>
steamcmd (Path to steamcmd; download from https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip)<br/>
steam_username (The account that has purchased Wallpaper Engine)<br/>
<br/>
Before running the program for the first time, unzip steamcmd into a folder, then run `cmd` or `PowerShell` in that path, execute `steamcmd.exe` or `.\steamcmd.exe`, then run `login &lt;username&gt; &lt;password&gt;`. You will be prompted to enter the mobile authenticator / email verification code. Once logged in successfully, the login information will be recorded in steamcmd<br/>
Wallpaper Engine - 壁纸定时自动切换应用程序<br/>
通过steamcmd自动下载创意工坊文件并让we应用来定时切换壁纸<br/>
config需要输入参数:<br/>
api_key（steam的web api_key，在https://steamcommunity.com/dev/apikey 获取）<br/>
interval（定时周期：如 3h / 90m / 1h30m；留空则只执行一次）<br/>
filters内参数请自行判断所需内容，为了防止赛博花柳病，types建议只填入Scene, Video<br/>
we_exe（wallpaper engine的程序路径，在steam中右键，-管理-浏览本地文件查看）<br/>
workshop_root（让we应用的壁纸留存的路径，建议不使用官方的workshop路径，防止壁纸删除的时候删掉创意工坊订阅的壁纸）<br/>
steamcmd（steamcmd 路径,需要先下载steamcmd：https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip ）<br/>
steam_username：（需要登录的购买了wallpaper engine的账号）<br/>
第一次程序运行之前，先解压steamcmd放入某个文件夹，然后在该路径运行cmd或者powershell，运行steamcmd.exe或.\steamcmd.exe，然后login 用户名 密码，会要求输入手机令牌/邮件验证，登录成功后，登录信息就记录在steamcmd<br/>
