
import os,sys,warnings
os.environ["TOKENIZERS_PARALLELISM"] = "false" 
os.environ['CUDA_MODULE_LOADING'] = 'LAZY'
warnings.simplefilter(action='ignore', category=FutureWarning)
warnings.simplefilter(action='ignore', category=UserWarning)
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from get_data_utils import *
import numpy as np
from tqdm.auto import tqdm
import cv2
from torch.utils.data import Dataset, TensorDataset, DataLoader
from dataaccelerate import DataPrefetcher 
from task_rec.batch_text_rec import TextRecognizer, rec_args
import torch
from scihub_pdf_dataset import RecImageDataset,rec_collate_fn,deal_with_one_pdf,none_collate_fn,clean_pdf_path,Timers
try:
    client=build_client()
except:
    client=None
eps=1e-7
import math

# def rec_preprocessing(text_recognizer, img_list):
#     norm_img_batch = []
    
#     resize_norm_img_func = partial(resize_norm_img,
#                                max_wh_ratio=max_wh_ratio,
#                                rec_image_shape  =text_recognizer.rec_image_shape,
#                                limited_max_width=text_recognizer.limited_max_width,
#                                limited_min_width=text_recognizer.limited_min_width)
#     for img_now in tqdm(img_list, desc="resize and normlized image"):
#         norm_img = resize_norm_img_func(img_now)
#         norm_img = norm_img[np.newaxis, :]
#         norm_img_batch.append(norm_img)
#     norm_img_batch = np.concatenate(norm_img_batch)
#     # norm_img_batch = norm_img_batch.copy()
#     return norm_img_batch

def resize_norm_img(img, max_wh_ratio=None,rec_image_shape=None,limited_max_width=None,limited_min_width=None):
    imgC, imgH, imgW = rec_image_shape
    assert imgC == img.shape[2]
    max_wh_ratio = max(max_wh_ratio, imgW / (imgH+eps))
    imgW = int((imgH * max_wh_ratio))
    imgW = max(min(imgW, limited_max_width), limited_min_width)
    h, w = img.shape[:2]
    ratio = w / (float(h)+eps)
    ratio_imgH = math.ceil(imgH * ratio)
    ratio_imgH = max(ratio_imgH, limited_min_width)
    if ratio_imgH > imgW:
        resized_w = imgW
    else:
        resized_w = int(ratio_imgH)
    resized_image = cv2.resize(img, (resized_w, imgH))
    resized_image = resized_image.astype('float32')
    resized_image = resized_image.transpose((2, 0, 1)) / 255
    resized_image -= 0.5
    resized_image /= 0.5
    padding_im = np.zeros((imgC, imgH, imgW), dtype=np.float32)
    padding_im[:, :, 0:resized_w] = resized_image
    return padding_im

class UnifiedResizedDataset(Dataset):
    def __init__(self, img_list,rec_image_shape,limited_max_width,limited_min_width):
        max_wh_ratio = 0
        for img_now in img_list:
            # h, w = img_list[ino].shape[0:2]
            h, w = img_now.shape[0:2]
            wh_ratio = w * 1.0 / (h+eps)
            max_wh_ratio = max(max_wh_ratio, wh_ratio)
        self.max_wh_ratio = max_wh_ratio
        self.image_list   = img_list
        self.rec_image_shape =rec_image_shape
        self.limited_max_width =limited_max_width
        self.limited_min_width =limited_min_width
    def __len__(self):
        return len(self.image_list)

    def __getitem__(self, idx):
        return idx, resize_norm_img(self.image_list[idx], self.max_wh_ratio, self.rec_image_shape, self.limited_max_width, self.limited_min_width)

class UnifiedResizedGroupDataset(Dataset):
    def __init__(self, img_list,rec_image_shape,limited_max_width,limited_min_width,max_wh_ratios_list):
    
        self.image_list   = img_list
        self.rec_image_shape =rec_image_shape
        self.limited_max_width =limited_max_width
        self.limited_min_width =limited_min_width
        self.max_wh_ratios_list = max_wh_ratios_list
    def __len__(self):
        return len(self.image_list)

    def __getitem__(self, idx):
        return idx, resize_norm_img(self.image_list[idx], self.max_wh_ratios_list[idx], self.rec_image_shape, self.limited_max_width, self.limited_min_width)



def postprocess(self,preds, label=None):
    preds_prob,preds_idx  = preds.max(axis=2)
    text = self.decode(preds_idx.cpu().numpy(), preds_prob.cpu().numpy(), is_remove_duplicate=True)

    if label is None:return text
    label = self.decode(label)
    return text, label

def gpu_inference(batch, tex_recognizer):
    inp = batch
    with torch.no_grad():
        with torch.cuda.amp.autocast(dtype=torch.float16): ### tested, fp16 only influence the result for last end sign like `.` or similar symbol like `0`` and `O`
            prob_out = tex_recognizer.net(inp)
    rec_result = postprocess(tex_recognizer.postprocess_op,prob_out)
    return rec_result


def calculate_dimensions(bbox):
        x_coords = bbox[::2]
        y_coords = bbox[1::2]
        width = max(x_coords) - min(x_coords)
        height = max(y_coords) - min(y_coords)
        return width, height


def build_bbox_group(metadatas, dataset):
    width_range = 100
    height_range= 100
    grouped_bboxes = {}
    location2group = {}
    location2boxes = {}
    count_how_many_pdf_is_recalculated  = {}
    count_how_many_page_is_recalculated = {}
    for pdf_index, pdf_metadata in enumerate(tqdm(metadatas,desc="building group")):
        pdf_path = clean_pdf_path(pdf_metadata['path'])
        for pdf_page_metadata in tqdm(pdf_metadata['doc_layout_result'],desc="iter along page", leave=False, position=1):
            location_keys = dataset.collect_location_and_dt_box_from_page_metadata(pdf_path, pdf_page_metadata)
            for location in location_keys:
                pdf_path,page_id,bbox_id,sub_box_id = location
                bbox = sub_box_id
                width, height = calculate_dimensions(bbox)
                width_group   = int(width  / (width_range + eps))
                height_group  = int(height / (height_range+ eps))
                group_key     = (width_group, height_group)
                if group_key not in grouped_bboxes:
                    grouped_bboxes[group_key] = []
                grouped_bboxes[group_key].append(location)
                location2group[location] = group_key
                location2boxes[location] = bbox
                count_how_many_pdf_is_recalculated[pdf_path] = 1
                count_how_many_page_is_recalculated[(pdf_path,page_id)] = 1
    count_how_many_pdf_is_recalculated = len(count_how_many_pdf_is_recalculated)
    count_how_many_page_is_recalculated = len(count_how_many_page_is_recalculated)
    count_how_many_box_is_recalculated = len(location2group)
    print(f"Processing: pdfs:{count_how_many_pdf_is_recalculated}, pages:{count_how_many_page_is_recalculated}, boxes:{count_how_many_box_is_recalculated}")
    return grouped_bboxes, location2group, location2boxes

from typing import List, Dict
def obtain_data_from_pool_list(pool_list, key):
    for pool in pool_list:
        if key in pool:
            return pool[key]
    return None

def deal_with_one_dataset(pdf_path, result_path, tex_recognizer,
                          pdf_batch_size  =32,
                          image_batch_size=256,
                          num_workers=8,
                          partion_num = 1,
                          partion_idx = 0,update_origin=False):
    images_dataset = RecImageDataset(pdf_path,partion_num = partion_num, partion_idx = partion_idx)
    data_to_save =  fast_deal_with_one_dataset2(images_dataset,tex_recognizer,
                                               pdf_batch_size  =pdf_batch_size,
                          image_batch_size=image_batch_size,num_workers=num_workers,
                                                          update_origin=update_origin)
    if data_to_save is not None:
        write_jsonl_to_path(data_to_save,result_path,images_dataset.client)



def fast_deal_with_one_dataset(images_dataset:RecImageDataset,tex_recognizer:TextRecognizer,
                          pdf_batch_size  =32,
                          image_batch_size=256,
                          num_workers=8, update_origin=False):

    _,location2group,location2boxes = build_bbox_group(images_dataset.metadata,images_dataset)
    image_collecter   = DataLoader(images_dataset, batch_size=pdf_batch_size,collate_fn=none_collate_fn, 
                            num_workers=num_workers,pin_memory=False,
                            prefetch_factor=2)  
    location_to_rec = {}
    for image_pool_list in tqdm(image_collecter,position=0,leave=True,desc="Images batch"):
        no_image_pdf_list = []
        image_pool = {}
        current_group_bboxes = {}
        for idx,(pdf_path, image_dict) in enumerate(tqdm(image_pool_list,position=0,leave=False, desc="Partiton current image pool")):
            if len(image_dict)==0:
                no_image_pdf_list.append(pdf_path)
                #print(f"pdf {pdf_path} has no text image")
                continue
            for key,val in image_dict.items():
                image_pool[key]=val
                group = location2group[key]
                if group not in current_group_bboxes:
                    current_group_bboxes[group] = []
                current_group_bboxes[group].append((key,location2boxes[key]))
        if len(image_pool) == 0:continue
        
        
        #### next step, lets do normlized the bbox to the same size

        
        pbar_whole_images  = tqdm(total=len(image_pool),position=1,leave=False,desc=f"Group batch:{len(no_image_pdf_list)} pdfs has no text image and {len(image_pool)} text images")
        for group_key, location_and_bbox in current_group_bboxes.items():
            if len(location_and_bbox) == 0:continue
            
            img_list_group = [image_pool[location] for location, bbox in location_and_bbox]
            rec_list_group = []
            dataset          = UnifiedResizedDataset(img_list_group, tex_recognizer.rec_image_shape, tex_recognizer.limited_max_width, tex_recognizer.limited_min_width)
            if len(dataset)<=image_batch_size:
                adapat_num_workers = 0
            elif len(dataset)<=2*image_batch_size:
                adapat_num_workers = 1
            else:
                adapat_num_workers = num_workers
            dataloader_group = DataLoader(dataset, batch_size=image_batch_size, num_workers=adapat_num_workers, pin_memory=True, pin_memory_device='cuda')
            featcher   = DataPrefetcher(dataloader_group,device='cuda')
            pbar  = tqdm(total=len(dataloader_group),position=2,leave=False,desc="GPU batch")
            batch = featcher.next()
            indexes=[]
            while batch is not None:
                index, batch = batch
                rec_result = gpu_inference(batch, tex_recognizer)
                rec_list_group.extend(rec_result)
                indexes.extend([t.item() for t in index])
                pbar.update(1)
                batch = featcher.next()
            assert len(location_and_bbox) == len(rec_list_group)

            for index, rec_res in zip(indexes, rec_list_group):
                (location, bbox) = location_and_bbox[index]
                location_to_rec[location] = rec_res

            pbar_whole_images.update(len(img_list_group))

    location_and_sub_location_map = {}
    for abs_location in location_to_rec.keys():
        pdf_path,page_id,bbox_id,sub_box_id = abs_location
        location = (pdf_path,page_id,bbox_id)
        if location not in location_and_sub_location_map:location_and_sub_location_map[location] = []
        location_and_sub_location_map[location].append(sub_box_id)

    
    patch_metadata_list = []
    for pdf_index, pdf_metadata in enumerate(tqdm(images_dataset.metadata)):
        pdf_path = clean_pdf_path(pdf_metadata['path'])
        
        patch_metadata = {'path':pdf_path,'doc_layout_result':[]}
        for pdf_page_metadata in pdf_metadata['doc_layout_result']:
            page_id = pdf_page_metadata['page_id']
            
            this_line_pool = {'page_id':page_id, 'layout_dets':[]}
            for bbox_metadata in pdf_page_metadata['layout_dets']:
                if bbox_metadata['category_id']!=15:continue
                bbox_id     = tuple(bbox_metadata['poly'])
                location    = (pdf_path,page_id,bbox_id)
                current_line_box_rec_result = []
                rel_location_list = location_and_sub_location_map[location]
                for sub_box_id in rel_location_list:
                    abs_location = (pdf_path,page_id,bbox_id,sub_box_id)
                    text, score  = location_to_rec[abs_location]
                    current_line_box_rec_result.append({'poly':sub_box_id, 'text':text, 'score':float(score)})
                if len(current_line_box_rec_result)==0:
                    continue
                if update_origin:
                    bbox_metadata.update({'sub_boxes':current_line_box_rec_result})
                else:
                    this_line_pool['layout_dets'].append({'category_id':15, 'sub_boxes':current_line_box_rec_result})
            patch_metadata['doc_layout_result'].append(this_line_pool)
        patch_metadata_list.append(patch_metadata)
    if update_origin:
        return images_dataset.metadata
    else:
        return patch_metadata_list


from torch.utils.data import Sampler

from torch.utils.data import Sampler

class GroupBatchSampler(Sampler):
    def __init__(self, group_indices, batch_size):
        self.group_indices = group_indices
        self.batch_size = batch_size

    def __iter__(self):
        for indices in self.group_indices:
            # Yield full batches within the group
            for i in range(0, len(indices), self.batch_size):
                yield indices[i:i + self.batch_size]

    def __len__(self):
        return sum((len(indices) + self.batch_size - 1) // self.batch_size for indices in self.group_indices)
    
def fast_deal_with_one_dataset2(images_dataset:RecImageDataset,tex_recognizer:TextRecognizer,
                          pdf_batch_size  =32,
                          image_batch_size=256,
                          num_workers=8,update_origin=False):
    
    _,location2group,location2boxes = build_bbox_group(images_dataset.metadata,images_dataset)
    if len(location2group) == 0:return None
    image_collecter   = DataLoader(images_dataset, batch_size=pdf_batch_size,collate_fn=none_collate_fn, 
                            num_workers=num_workers,pin_memory=False,
                            prefetch_factor=2)  
    location_to_rec = {}
    for image_pool_list in tqdm(image_collecter,position=1,leave=True,desc="Images batch"):
        no_image_pdf_list = []
        image_pool = {}
        current_group_bboxes = {}
        for idx,(pdf_path, image_dict) in enumerate(tqdm(image_pool_list,position=2,leave=False, desc="Partiton current image pool")):
            if len(image_dict)==0:
                no_image_pdf_list.append(pdf_path)
                #print(f"pdf {pdf_path} has no text image")
                continue
            for key,val in image_dict.items():
                image_pool[key]=val
                group = location2group[key]
                if group not in current_group_bboxes:
                    current_group_bboxes[group] = []
                current_group_bboxes[group].append((key,location2boxes[key]))
        if len(image_pool) == 0:continue
        
        
        #### next step, lets do normlized the bbox to the same size

        all_images = []
        all_max_wh_ratios = []
        group_indices = []
        location_bbox_map = []
        current_index = 0
        for group_key, location_and_bbox in current_group_bboxes.items():
            if len(location_and_bbox) == 0:
                continue

            img_list_group = [image_pool[location] for location, bbox in location_and_bbox]
            max_wh_ratio = max((w / (h + 1e-5) for img in img_list_group for h, w in [img.shape[:2]]), default=0)

            all_images.extend(img_list_group)
            all_max_wh_ratios.extend([max_wh_ratio] * len(img_list_group))
            location_bbox_map.extend(location_and_bbox)

            group_indices.append(list(range(current_index, current_index + len(img_list_group))))
            current_index += len(img_list_group)
        
        dataset       = UnifiedResizedGroupDataset(all_images, tex_recognizer.rec_image_shape, tex_recognizer.limited_max_width, tex_recognizer.limited_min_width, all_max_wh_ratios)
        batch_sampler = GroupBatchSampler(group_indices,image_batch_size)
        dataloader    = DataLoader(dataset, batch_sampler=batch_sampler, num_workers=num_workers, pin_memory=True, pin_memory_device='cuda')



        featcher   = DataPrefetcher(dataloader ,device='cuda')
        pbar = tqdm(total=len(dataset), position=2, leave=False, desc="GPU batch")
        batch = featcher.next()
        indexes=[]
        rec_list = []
        while batch is not None:
            index, batch = batch
            #tqdm.write(f"This Batch shape is {batch.shape}")
            rec_result = gpu_inference(batch, tex_recognizer)
            rec_list.extend(rec_result)
            indexes.extend([t.item() for t in index])
            pbar.update(len(batch))
            batch = featcher.next()

        assert len(rec_list) == len(location_bbox_map)
        for index, rec_res in zip(indexes, rec_list):
            (location, bbox) = location_bbox_map[index]
            location_to_rec[location] = rec_res


    location_and_sub_location_map = {}
    for abs_location in location_to_rec.keys():
        pdf_path,page_id,bbox_id,sub_box_id = abs_location
        location = (pdf_path,page_id,bbox_id)
        if location not in location_and_sub_location_map:location_and_sub_location_map[location] = []
        location_and_sub_location_map[location].append(sub_box_id)

    
    patch_metadata_list = []
    for pdf_index, pdf_metadata in enumerate(tqdm(images_dataset.metadata)):
        pdf_path = clean_pdf_path(pdf_metadata['path'])
        
        patch_metadata = {'path':pdf_path,'doc_layout_result':[]}
        for pdf_page_metadata in pdf_metadata['doc_layout_result']:
            page_id = pdf_page_metadata['page_id']
            
            this_line_pool = {'page_id':page_id, 'layout_dets':[]}
            for bbox_metadata in pdf_page_metadata['layout_dets']:
                if bbox_metadata['category_id']!=15:continue
                
                bbox_id     = tuple(bbox_metadata['poly'])
                location    = (pdf_path,page_id,bbox_id)
                if location not in location_and_sub_location_map:
                    assert update_origin, "you must update the origin metadata if you choose skip some bbox"
                    continue
                current_line_box_rec_result = []
                rel_location_list = location_and_sub_location_map[location]
                for sub_box_id in rel_location_list:
                    abs_location = (pdf_path,page_id,bbox_id,sub_box_id)
                    text, score  = location_to_rec[abs_location]

                    sub_box_id = tuple([int(t) for t in sub_box_id])

                    current_line_box_rec_result.append({'poly':sub_box_id, 'text':text, 'score':float(score)})
                if len(current_line_box_rec_result)==0:
                    continue
                if update_origin:
                    bbox_metadata.update({'sub_boxes':current_line_box_rec_result})
                else:
                    this_line_pool['layout_dets'].append({'category_id':15, 'sub_boxes':current_line_box_rec_result})
            patch_metadata['doc_layout_result'].append(this_line_pool)
        patch_metadata_list.append(patch_metadata)
    if update_origin:
        return images_dataset.metadata
    else:
        return patch_metadata_list


if __name__ == "__main__":
    
    ocr_mode = 'batch'
    batch_size = 128
    num_workers= 8
    metadata_filepath = "0000000-0000209.01000_00001.jsonl"
    images_dataset    = RecImageDataset(metadata_filepath)
    # _,location2group,location2boxes = build_bbox_group(images_dataset.metadata,images_dataset)
    # image_collecter   = DataLoader(images_dataset, batch_size=2,collate_fn=none_collate_fn, 
    #                         num_workers=num_workers,pin_memory=False,
    #                         prefetch_factor=2)  
  
    # for image_pool_list in tqdm(image_collecter,position=1,leave=True,desc="Images batch"):
    #     no_image_pdf_list = []
    #     image_pool = {}
    #     current_group_bboxes = {}
    #     for idx,(pdf_path, image_dict) in enumerate(tqdm(image_pool_list,position=2,leave=False, desc="Partiton current image pool")):
    #         if len(image_dict)==0:
    #             no_image_pdf_list.append(pdf_path)
    #             #print(f"pdf {pdf_path} has no text image")
    #             continue
    #         for key,val in image_dict.items():
    #             image_pool[key]=val
    #             group = location2group[key]
    #             if group not in current_group_bboxes:
    #                 current_group_bboxes[group] = []
    #             current_group_bboxes[group].append((key,location2boxes[key]))
    #     if len(image_pool) == 0:continue
    #     print(len(image_pool))
    # raise
    if ocr_mode == 'batch':
        tex_recognizer = TextRecognizer(rec_args)
        #tex_recognizer.net.backbone = torch.compile(tex_recognizer.net.backbone)
        patch_metadata_list = fast_deal_with_one_dataset2(images_dataset,tex_recognizer,pdf_batch_size=32, 
                                                          image_batch_size=128 ,
                                                          num_workers=num_workers,
                                                          update_origin=True)
        #print(patch_metadata_list)
        write_jsonl_to_path(patch_metadata_list, "test_result/result.test3.jsonl", None)
        # patch_metadata_list = fast_deal_with_one_dataset(images_dataset,tex_recognizer,pdf_batch_size=32, image_batch_size=128 ,num_workers=num_workers)
        # write_jsonj_to_path(patch_metadata_list, "test_result/result.test1.jsonl", None)
    else:
        from modules.self_modify import ModifiedPaddleOCR
        
        dataset           = RecImageDataset(metadata_filepath)
        image_collecter   = DataLoader(dataset, batch_size=8,collate_fn=rec_collate_fn, 
                                num_workers=num_workers,pin_memory=False, pin_memory_device='cuda',
                                prefetch_factor=2 if num_workers>0 else None)  
    
        ocr_model = ModifiedPaddleOCR(show_log=True)
        tex_recognizer=ocr_model.text_recognizer
        # tex_recognizer = TextRecognizer(rec_args)
        tex_recognizer.rec_batch_num = batch_size
        for location_abs_list, image_list in tqdm(image_collecter,position=0,leave=False,desc="Do Rec"):
            if len(image_list) == 0:continue
            tqdm.write(f"Now deal with B={len(image_list)}")
            rec_result = tex_recognizer(image_list)
            
    
    
    
    
    # #### next step, lets do normlized the bbox to the same size

    # location_to_rec = {}
    # pbar_whole_images  = tqdm(total=len(image_pool),position=1,leave=False)
    # for group_key, location_and_bbox in grouped_bboxes.items():
    #     if len(location_and_bbox) == 0:continue
        
    #     img_list_group = [image_pool[location] for location, bbox in location_and_bbox]
    #     rec_list_group = []
    #     dataset  = UnifiedResizedDataset(img_list_group, tex_recognizer.rec_image_shape, tex_recognizer.limited_max_width, tex_recognizer.limited_min_width)
    #     dataloader_group = DataLoader(dataset, batch_size=batch_size, num_workers=8, pin_memory=True, pin_memory_device='cuda')
    #     featcher   = DataPrefetcher(dataloader_group,device='cuda')
    #     pbar  = tqdm(total=len(dataloader_group),position=2,leave=False)
    #     batch = featcher.next()
    #     while batch is not None:
    #         rec_result = gpu_inference(batch, tex_recognizer)
    #         rec_list_group.extend(rec_result)
    #         pbar.update(1)
    #         batch = featcher.next()
    #     assert len(location_and_bbox) == len(rec_list_group)
    #     for (location, bbox), rec_res in zip(location_and_bbox, rec_list_group):
    #         location_to_rec[location] = rec_res
    #     pbar_whole_images.update(len(img_list_group))

    # patch_metadata_list = []
    # for pdf_index, pdf_metadata in enumerate(tqdm(metadatas)):
    #     pdf_path = pdf_metadata['path']
        
    #     patch_metadata = {'path':pdf_path,'doc_layout_result':[]}
    #     for pdf_page_metadata in pdf_metadata['doc_layout_result']:
    #         page_id = pdf_page_metadata['page_id']
    #         bbox_id = 0
    #         this_line_pool = {'page_id':page_id, 'layout_dets':[]}
    #         for bbox_metadata in pdf_page_metadata['layout_dets']:
    #             if bbox_metadata['category_id']!=15:continue
                
    #             location= (pdf_path,page_id,bbox_id)
    #             bbox_id+=1
    #             text, score = location_to_rec[location]
    #             this_line_pool['layout_dets'].append({'category_id':15, 'text':text, 'score':score})
    #         patch_metadata['doc_layout_result'].append(this_line_pool)
    #     patch_metadata_list.append(patch_metadata)
    
    # write_json_to_path(patch_metadata_list, metadata_filepath.replace('.jsonl','.patch.rec_result.jsonl'), client)

    # deal_with_one_dataset("debug.jsonl", 
    #                       "debug.stage_1.jsonl", 
    #                       layout_model, mfd_model, ocrmodel=ocrmodel, 
    #                       inner_batch_size=2, batch_size=4,num_workers=4,
    #                       do_text_det = True,
    #                       do_text_rec = True,
    #                       timer=timer)
    # dataset    = PDFImageDataset("part-66210c190659-000035.jsonl",layout_model.predictor.aug,layout_model.predictor.input_format,mfd_pre_transform=None)
    # dataloader = DataLoader(dataset, batch_size=8,collate_fn=custom_collate_fn)  

    
    