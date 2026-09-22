// 列出相机通过 AVFoundation 暴露的**全部**采集格式。
//
// 为什么需要它：OpenCV 在 macOS 上不枚举 UVC 模式，只按宽高去猜，
// 实测要 2880x1440 却给了 1552x1552；而这个 ffmpeg 构建又砍掉了 -list_formats。
// AVFoundation 才是唯一可信的来源。
//
// 用法：avformats [名字或 uniqueID 片段]

#import <AVFoundation/AVFoundation.h>
#import <Foundation/Foundation.h>

static NSString *fourcc_str(FourCharCode c) {
    char s[5] = {(char)((c >> 24) & 0xff), (char)((c >> 16) & 0xff),
                 (char)((c >> 8) & 0xff), (char)(c & 0xff), 0};
    for (int i = 0; i < 4; i++) {
        if (s[i] < 32 || s[i] > 126) s[i] = '.';
    }
    return [NSString stringWithFormat:@"%s", s];
}

int main(int argc, char *argv[]) {
    @autoreleasepool {
        NSString *want = (argc > 1) ? [NSString stringWithUTF8String:argv[1]] : nil;

        NSArray<AVCaptureDevice *> *devices =
            [AVCaptureDevice devicesWithMediaType:AVMediaTypeVideo];

        printf("视频采集设备共 %lu 个\n\n", (unsigned long)devices.count);

        for (AVCaptureDevice *d in devices) {
            NSString *name = d.localizedName ?: @"(无名)";
            if (want && ![name localizedCaseInsensitiveContainsString:want] &&
                ![d.uniqueID localizedCaseInsensitiveContainsString:want]) {
                continue;
            }

            printf("设备: %s\n", name.UTF8String);
            printf("  uniqueID : %s\n", d.uniqueID.UTF8String);
            printf("  modelID  : %s\n", (d.modelID ?: @"-").UTF8String);
            printf("  格式数   : %lu\n", (unsigned long)d.formats.count);

            // 按分辨率排序，2:1 的档位一眼可见
            NSArray<AVCaptureDeviceFormat *> *formats = [d.formats sortedArrayUsingComparator:
                ^NSComparisonResult(AVCaptureDeviceFormat *a, AVCaptureDeviceFormat *b) {
                  CMVideoDimensions da = CMVideoFormatDescriptionGetDimensions(a.formatDescription);
                  CMVideoDimensions db = CMVideoFormatDescriptionGetDimensions(b.formatDescription);
                  if (da.width != db.width) return da.width < db.width ? NSOrderedAscending : NSOrderedDescending;
                  if (da.height != db.height) return da.height < db.height ? NSOrderedAscending : NSOrderedDescending;
                  return NSOrderedSame;
                }];

            for (AVCaptureDeviceFormat *f in formats) {
                CMVideoDimensions dim = CMVideoFormatDescriptionGetDimensions(f.formatDescription);
                FourCharCode sub = CMFormatDescriptionGetMediaSubType(f.formatDescription);

                NSMutableString *rates = [NSMutableString string];
                for (AVFrameRateRange *r in f.videoSupportedFrameRateRanges) {
                    if (rates.length) [rates appendString: @", "];
                    if (r.minFrameRate == r.maxFrameRate) {
                        [rates appendFormat:@"%.0f", r.maxFrameRate];
                    } else {
                        [rates appendFormat:@"%.1f-%.1f", r.minFrameRate, r.maxFrameRate];
                    }
                }

                double ar = dim.height ? (double)dim.width / (double)dim.height : 0.0;
                const char *tag = (fabs(ar - 2.0) < 0.02) ? "  <== 2:1 全景"
                                  : (fabs(ar - 1.0) < 0.02) ? "  (方形)"
                                                            : "";
                printf("    %5ux%-5u  %-6s  fps: %-18s  比例 %.2f%s\n",
                       dim.width, dim.height, fourcc_str(sub).UTF8String,
                       rates.UTF8String, ar, tag);
            }
            printf("\n");
        }
    }
    return 0;
}
