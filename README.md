# wallpaper_auto_downloader
Wallpaper Engine – Wallpaper Scheduled Automatic Switching Application<br/>
Automatically download Workshop items via steamcmd and use the WE application to switch wallpapers on a schedule<br/>
Because Steam prohibits automated programs from logging in without a head, and there is no API for automatically subscribing to the workshop, you can only rely on steamcmd for downloading and cannot subscribe.<br/>
The config requires the following parameters:<br/>
api_key (Steam Web API key, obtained from https://steamcommunity.com/dev/apikey)<br/>
interval (Scheduling interval, e.g., 3h / 90m / 1h30m; leave empty to execute only once)<br/>
filters (Set parameters according to your needs; to avoid inappropriate content, it is recommended to only include `Scene` and `Video` in `types`)<br/>
we_exe (Path to the Wallpaper Engine executable; right-click in Steam → Manage → Browse Local Files to find)<br/>
workshop_root (Path where WE will store wallpapers; it is recommended not to use the official Workshop path to prevent deletion of subscribed wallpapers)<br/>
steamcmd (Path to steamcmd; download from https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip)<br/>
steam_username (The account that has purchased Wallpaper Engine)<br/>
Before running the program for the first time, unzip steamcmd into a folder, then run `cmd` or `PowerShell` in that path, execute `steamcmd.exe` or `.\steamcmd.exe`, then run `login username password`. You will be prompted to enter the mobile authenticator / email verification code. Once logged in successfully, the login information will be recorded in steamcmd<br/>
After the program starts, an icon will be displayed in the application tray. Right-click to set whether the application starts automatically when the computer is turned on, immediately change wallpaper; left-click to view console (to check if logging in and downloading wallpapers are normal). It is recommended not to change wallpapers too frequently as it may trigger Steam's anti-scraping mechanism.<br/>
Due to steamcmd being a separate login for Steam, it is not possible to subscribe to Workshop files in Wallpaper Engine while the program is running. If you must subscribe to Workshop files while the program is running, please purchase two copies of Wallpaper Engine with two different accounts and log into Steam and steamcmd with different accounts. Use the account that steamcmd logs in with as "steam_username".<br/>
<br/>
Wallpaper Engine - 壁纸定时自动切换应用程序<br/>
通过steamcmd自动下载创意工坊文件并让we应用来定时切换壁纸<br/>
因为steam禁止自动化程序进行无头登录，且也没有自动订阅创意工坊的api，所以只能依靠steamcmd下载，不能订阅<br/>
config需要输入参数:<br/>
api_key（steam的web api_key，在https://steamcommunity.com/dev/apikey 获取）<br/>
interval（定时周期：如 3h / 90m / 1h30m；留空则只执行一次）<br/>
filters内参数请自行判断所需内容，为了防止赛博花柳病，types建议只填入Scene, Video<br/>
we_exe（wallpaper engine的程序路径，在steam中右键，-管理-浏览本地文件查看）<br/>
workshop_root（让we应用的壁纸留存的路径，建议不使用官方的workshop路径，防止壁纸删除的时候删掉创意工坊订阅的壁纸）<br/>
steamcmd（steamcmd 路径,需要先下载steamcmd：https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip ）<br/>
steam_username：（需要登录的购买了wallpaper engine的账号）<br/>
第一次程序运行之前，先解压steamcmd放入某个文件夹，然后在该路径运行`cmd`或者`powershell`，运行`steamcmd.exe`或`.\steamcmd.exe`，然后`login 用户名 密码`，会要求输入手机令牌/邮件验证，登录成功后，登录信息就记录在steamcmd<br/>
程序启动后，会在应用托盘显示一个图标，右键可以设置应用是否开机自启，立即更换壁纸，左键为查看控制台（可以排查是否正常登录和下载了壁纸），建议不要更换壁纸太过频繁，可能触发steam反爬机制<br/>
由于steamcmd是独立的steam登录，程序运行期间无法正常在wallpaper engine内订阅创意工坊文件，如果非要在程序运行时订阅创意工坊文件，请在两个账号购买两份wallpaper engine然后让steam和steamcmd登录不同的账号，steam_username使用steamcmd登录的账号<br/>
