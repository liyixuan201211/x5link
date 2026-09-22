// X5Link.app 的真正入口。
//
// 为什么必须有这个原生程序：
//   OpenCV 的 AVFoundation 后端在申请摄像头权限时是「发个请求然后死等」，
//   它自己把主线程 run loop 堵住了，于是授权弹窗根本弹不出来，
//   结果是永远停在 status=0（未决定），既不报错也不授权。
//   这个程序用正确的方式申请：一边等信号量、一边转 run loop，
//   弹窗才有机会出现、用户点了「允许」才会被系统记下来。
//
// 申请到权限后，它不 exec，而是把 python 工具作为**子进程**拉起并等待。
// 保持 bundle 进程为父进程，macOS 才能把子进程的摄像头调用归到本 bundle 上。

#import <AVFoundation/AVFoundation.h>
#import <AppKit/AppKit.h>
#import <Foundation/Foundation.h>

#include <stdio.h>

static AVAuthorizationStatus ensure_access(AVMediaType type, const char *label) {
    AVAuthorizationStatus st = [AVCaptureDevice authorizationStatusForMediaType:type];

    if (st == AVAuthorizationStatusNotDetermined) {
        fprintf(stderr, "[X5Link] 正在申请%s权限，请在弹窗中点「允许」...\n", label);
        dispatch_semaphore_t sem = dispatch_semaphore_create(0);
        [AVCaptureDevice requestAccessForMediaType:type
                                 completionHandler:^(BOOL granted) {
                                   (void)granted;
                                   dispatch_semaphore_signal(sem);
                                 }];
        // 关键：等的时候必须转 run loop，否则弹窗无法响应
        while (dispatch_semaphore_wait(
                   sem, dispatch_time(DISPATCH_TIME_NOW, 100 * NSEC_PER_MSEC)) != 0) {
            [[NSRunLoop currentRunLoop] runMode:NSDefaultRunLoopMode
                                     beforeDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];
        }
        st = [AVCaptureDevice authorizationStatusForMediaType:type];
    }

    const char *name = (st == AVAuthorizationStatusAuthorized)   ? "已授权"
                       : (st == AVAuthorizationStatusDenied)     ? "被拒绝"
                       : (st == AVAuthorizationStatusRestricted) ? "受限制"
                                                                 : "仍未决定";
    fprintf(stderr, "[X5Link] %s权限：status=%ld (%s)\n", label, (long)st, name);
    return st;
}

int main(int argc, char *argv[]) {
    @autoreleasepool {
        // 关键一步：把进程变成「真正的 GUI App」。
        // 光是套一个 .app 壳还不够 —— 纯命令行进程没有 NSApplication，
        // 系统不会为它显示 TCC 授权弹窗（我们实测过：请求直接返回、窗口根本不出现）。
        [NSApplication sharedApplication];
        [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
        [NSApp activateIgnoringOtherApps:YES];

        ensure_access(AVMediaTypeVideo, "摄像头");

        NSString *bundle = [[NSBundle mainBundle] bundlePath];
        NSString *runner = [bundle stringByAppendingPathComponent:@"Contents/Resources/run.sh"];

        NSMutableArray<NSString *> *args = [NSMutableArray arrayWithObject:runner];
        for (int i = 1; i < argc; i++) {
            [args addObject:[NSString stringWithUTF8String:argv[i]]];
        }

        NSTask *task = [[NSTask alloc] init];
        task.launchPath = @"/bin/bash";
        task.arguments = args;
        task.standardOutput = [NSFileHandle fileHandleWithStandardOutput];
        task.standardError = [NSFileHandle fileHandleWithStandardError];

        @try {
            [task launch];
        } @catch (NSException *e) {
            fprintf(stderr, "[X5Link] 起不来子进程：%s\n", [[e description] UTF8String]);
            return 3;
        }
        [task waitUntilExit];
        return task.terminationStatus;
    }
}
